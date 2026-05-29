"""Tests for GET /health endpoint and RequestIdMiddleware."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentsys.config import get_settings
from agentsys.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Task 3.1 — Health all ok
# ---------------------------------------------------------------------------

async def test_health_all_ok(app):
    """Both postgres and redis healthy → status: ok."""
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

    with patch("agentsys.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"


# ---------------------------------------------------------------------------
# Task 3.2 — Postgres degraded
# ---------------------------------------------------------------------------

async def test_health_postgres_degraded(app):
    """Postgres raises → status: degraded, postgres: error."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(side_effect=Exception("DB unavailable"))
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    app.state.engine = mock_engine

    with patch("agentsys.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["postgres"] == "error"
    assert body["status"] == "degraded"
    assert body["redis"] == "ok"


# ---------------------------------------------------------------------------
# Task 3.3 — Redis degraded
# ---------------------------------------------------------------------------

async def test_health_redis_degraded(app):
    """Redis raises → status: degraded, redis: error."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=MagicMock())

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis unavailable"))

    app.state.engine = mock_engine

    with patch("agentsys.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["redis"] == "error"
    assert body["status"] == "degraded"
    assert body["postgres"] == "ok"


# ---------------------------------------------------------------------------
# Both dependencies degraded
# ---------------------------------------------------------------------------

async def test_health_both_degraded(app):
    """Both postgres and redis down → status: degraded, both: error."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(side_effect=Exception("DB unavailable"))
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis unavailable"))

    app.state.engine = mock_engine

    with patch("agentsys.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["postgres"] == "error"
    assert body["redis"] == "error"


# ---------------------------------------------------------------------------
# Task 3.4 — Middleware adds request_id to logs
# ---------------------------------------------------------------------------

async def test_middleware_adds_request_id(app):
    """RequestIdMiddleware binds request_id (8-char) to structlog contextvars."""
    log_lines: list[dict] = []
    captured = StringIO()

    import structlog

    # Capture structlog output by patching the renderer
    def capturing_renderer(logger, method, event_dict):  # type: ignore[type-arg]
        log_lines.append(dict(event_dict))
        return json.dumps(event_dict)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            capturing_renderer,
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=captured),
    )

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=MagicMock())

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()
    app.state.engine = mock_engine

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    with patch("agentsys.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.get("/health")

    # At least one log line must have a request_id with 8 chars
    assert log_lines, "No log lines were captured — middleware did not log"
    request_id_lines = [line for line in log_lines if "request_id" in line]
    assert request_id_lines, "No log lines contain 'request_id'"
    for line in request_id_lines:
        assert len(line["request_id"]) == 8, (
            f"Expected 8-char request_id, got: {line['request_id']!r}"
        )
