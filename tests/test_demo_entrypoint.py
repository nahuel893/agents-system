"""Smoke test for the demo API entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agents_system.config import Settings
from agents_system.demo import (
    _demo_registry_factory,
    _ensure_read_only_engine,
    build_app,
)
from agents_system.main import lifespan


def _fake_checkpointer_cm_factory(fake_checkpointer: Any) -> Any:
    """Build a callable matching `_build_checkpointer_cm(settings)`'s
    signature/return shape: an async context manager yielding a fake
    checkpointer, with no real Redis connection involved. Mirrors
    `tests/test_main.py::_fake_checkpointer_cm_factory`.
    """

    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield fake_checkpointer

    return _cm


async def test_demo_app_health_is_ok_without_lifespan() -> None:
    """The demo app exposes /health without connecting to real services."""
    app = build_app(engine=MagicMock(), model=MagicMock())

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=MagicMock())

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    app.state.engine = mock_engine

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# _ensure_read_only_engine -- startup read-only-role verification
# ---------------------------------------------------------------------------


async def test_ensure_read_only_engine_raises_when_not_read_only_and_insecure_not_allowed() -> (
    None
):
    """A demo DB role that can write must block startup by default."""
    with (
        patch("agents_system.demo.role_is_read_only", AsyncMock(return_value=False)),
        pytest.raises(SystemExit),
    ):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=False)


async def test_ensure_read_only_engine_logs_and_continues_when_allow_insecure() -> None:
    """ALLOW_INSECURE=true bypasses the block but must not raise."""
    with patch("agents_system.demo.role_is_read_only", AsyncMock(return_value=False)):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=True)


async def test_ensure_read_only_engine_continues_when_unverified() -> None:
    """An undetermined role (DB unreachable) must not fail closed, like BI's check."""
    with patch("agents_system.demo.role_is_read_only", AsyncMock(return_value=None)):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=False)


async def test_ensure_read_only_engine_continues_when_read_only() -> None:
    """A confirmed read-only role must not raise or warn about anything wrong."""
    with patch("agents_system.demo.role_is_read_only", AsyncMock(return_value=True)):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=False)


# ---------------------------------------------------------------------------
# _demo_registry_factory -- every expected demo tool is registered
# ---------------------------------------------------------------------------


def test_demo_registry_factory_registers_every_expected_tool() -> None:
    factory = _demo_registry_factory(engine=MagicMock(), model=MagicMock())
    registry = factory(settings=None)

    expected_tools = (
        "catalog_search",
        "client_lookup",
        "run_report",
        "message_sender",
        "knowledge_retrieval",
        "conversation_summarizer",
        "escalation_notifier",
        "order_writer",
        "session_state",
        "use_term",
        "read_file",
    )
    for tool_name in expected_tools:
        assert tool_name in registry


# ---------------------------------------------------------------------------
# build_app -- real DEPLOY_GRANTS example boots cleanly through the real
# agents_system.main.lifespan (docs/platform/demo-entrypoint.md, issue #38)
# ---------------------------------------------------------------------------


async def test_demo_build_app_boots_with_real_deploy_grants_through_lifespan() -> None:
    """The documented `AGENT_REGISTRATIONS`/`DEPLOY_GRANTS` example for the
    `demo-sales-agent` runtime id (docs/platform/demo-entrypoint.md) must
    actually boot: the demo's own
    `build_app` registry, wired through the REAL `agents_system.main.lifespan`,
    granted exactly the REAL `sales-agent` role's six declared permissions
    (`platform/roles/sales-agent/manifest.md`). None of those six permissions
    is in the `exec:`/`run:` (T3) family, so granting the role's full declared
    set to it here is safe even though it declares `untrusted_input: true`
    (ADR-002 C.11/C.13) -- no `UntrustedInputGrantError` is expected.

    `agents_system.harness.loader.resolve` and
    `agents_system.harness.factory.build_runtime` are deliberately left
    UNPATCHED, mirroring
    `tests/test_main.py::test_lifespan_boot_fails_for_untrusted_role_granted_t3_permission`
    -- mocking either would only prove a fake stood in for the real role,
    not that the actual documented example works.
    """
    test_settings = Settings(
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        agent_registrations={"demo-sales-agent": "sales-agent"},
        adapter_runtimes=["demo-sales-agent"],
        deploy_grants={
            "demo-sales-agent": (
                "read:catalog",
                "read:client_registry",
                "write:orders",
                "write:order_items",
                "read:price_lists",
                "send:message",
            )
        },
    )

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        app = build_app(engine=MagicMock(), model=MagicMock())

        async with lifespan(app):
            assert "demo-sales-agent" in app.state.runtimes
