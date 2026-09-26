"""Smoke test for the demo API entrypoint."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _demo_app import demo_app
from httpx import ASGITransport, AsyncClient

from agents_system.config import Settings
from agents_system.main import lifespan

# PR5 (ADR-004) moved `agents_system.demo` to `examples/demo/app.py`, which
# ships no `__init__.py` (design.md D7) and is loaded by path through
# `tests/_demo_app.py` instead of a normal import statement.
_demo_registry_factory = demo_app._demo_registry_factory
_ensure_read_only_engine = demo_app._ensure_read_only_engine
build_app = demo_app.build_app

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every doc that tells a reader to run `examples/demo/app.py` (README.md's
# "Run the Demo API" section, docs/platform/demo-entrypoint.md, and its ES
# twin). Every other command these docs give (`uv run python
# demo/load_demo_company.py`, `uv run pytest`, ...) is prefixed with
# `uv run` specifically so it works without the reader having activated
# `.venv` themselves; the invocation line must match that convention.
_DEMO_INVOCATION_DOCS = (
    "README.md",
    "docs/platform/demo-entrypoint.md",
    "docs/platform_es/demo-entrypoint.md",
)


# ---------------------------------------------------------------------------
# PR5-T2 review fix -- the "Run it" invocation must keep the `uv run` prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", _DEMO_INVOCATION_DOCS)
def test_demo_invocation_doc_keeps_uv_run_prefix(doc: str) -> None:
    """Every `python examples/demo/app.py` invocation (as opposed to a bare
    `examples/demo/app.py` file reference) must be spelled
    `uv run python examples/demo/app.py`, exactly like every other command
    in the same docs -- otherwise the documented command fails with
    `ModuleNotFoundError` for a reader who never activated `.venv`."""
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")

    unprefixed = re.findall(r"(?<!uv run )python examples/demo/app\.py", text)

    assert unprefixed == [], (
        f"{doc} invokes `python examples/demo/app.py` without the `uv run` "
        f"prefix used by every other command in this doc: found "
        f"{len(unprefixed)} occurrence(s)."
    )


# ---------------------------------------------------------------------------
# PR5-T1 review fix -- demo-entrypoint.md's own "generic role" leftover
# ---------------------------------------------------------------------------


def test_demo_entrypoint_doc_has_no_leftover_generic_role_text() -> None:
    """agent-definition-locator spec's Terminology section: the eight
    packaged platform roles are "predefined role(s)", never "generic
    role(s)". docs/platform/demo-entrypoint.md already says "predefined
    role" in its variables table, so a leftover "generic role" a few lines
    below is a completeness gap within this exact same document."""
    text = (_REPO_ROOT / "docs/platform/demo-entrypoint.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "generic role" not in normalized.lower()


def test_demo_entrypoint_doc_es_has_no_leftover_rol_generico_text() -> None:
    """ES twin of the check above: "rol genérico" must not remain once the
    table above it already says "rol predefinido"."""
    text = (_REPO_ROOT / "docs/platform_es/demo-entrypoint.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())

    assert "rol genérico" not in normalized.lower()


# ---------------------------------------------------------------------------
# PR5 (ADR-004) -- agents_system.demo relocates to examples/demo/app.py
# ---------------------------------------------------------------------------


def test_agents_system_demo_module_no_longer_exists() -> None:
    """agent-registration-serving spec: "The installed package contains no
    application entrypoint" -- `agents_system.demo` must not be importable
    once PR5 moves it under `examples/`."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents_system.demo")


def test_examples_demo_app_exposes_build_app_and_main() -> None:
    """agent-registration-serving spec: "An equivalent example entrypoint
    exists outside the package" -- `examples/demo/app.py` exposes the same
    `build_app`/`main` surface `agents_system.demo` did."""
    assert callable(demo_app.build_app)
    assert callable(demo_app.main)


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
        patch.object(demo_app, "role_is_read_only", AsyncMock(return_value=False)),
        pytest.raises(SystemExit),
    ):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=False)


async def test_ensure_read_only_engine_logs_and_continues_when_allow_insecure() -> None:
    """ALLOW_INSECURE=true bypasses the block but must not raise."""
    with patch.object(demo_app, "role_is_read_only", AsyncMock(return_value=False)):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=True)


async def test_ensure_read_only_engine_continues_when_unverified() -> None:
    """An undetermined role (DB unreachable) must not fail closed, like BI's check."""
    with patch.object(demo_app, "role_is_read_only", AsyncMock(return_value=None)):
        await _ensure_read_only_engine(MagicMock(), allow_insecure=False)


async def test_ensure_read_only_engine_continues_when_read_only() -> None:
    """A confirmed read-only role must not raise or warn about anything wrong."""
    with patch.object(demo_app, "role_is_read_only", AsyncMock(return_value=True)):
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
