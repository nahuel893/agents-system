"""Smoke test for the demo API entrypoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agents_system.demo import (
    _demo_registry_factory,
    _ensure_read_only_engine,
    build_app,
)


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
