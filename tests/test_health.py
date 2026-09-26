"""Tests for GET /health endpoint and RequestIdMiddleware."""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import create_test_app
from httpx import ASGITransport, AsyncClient

from agents_system.config import get_settings
from agents_system.main import ProviderHealthProbe
from agents_system.services.outbox import OutboxBacklogCounts


class FakeProviderProbe:
    """Offline provider-probe fake for health endpoint tests."""

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.probe = AsyncMock(side_effect=self._probe)

    async def _probe(self, settings: object) -> bool:
        return self.reachable


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app():
    application = create_test_app()
    application.state.runtimes = {"test": object()}
    application.state.provider_probe = FakeProviderProbe()
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
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

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")
            ready = await ac.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"
    assert body["provider"] == {"status": "ok", "reason": None}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"


async def test_health_skips_provider_probe_when_no_runtime_is_configured(app):
    """Offline deployments have no provider dependency to make readiness fail."""
    _, mock_redis = _mock_healthy_deps(app)
    del app.state.runtimes
    probe = app.state.provider_probe

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    assert response.json()["provider"] == {"status": "not_configured", "reason": None}
    probe.probe.assert_not_awaited()


async def test_health_and_ready_report_provider_outage_without_network(app):
    """A fake failed probe degrades readiness but never makes liveness 5xx."""
    _, mock_redis = _mock_healthy_deps(app)
    app.state.provider_probe = FakeProviderProbe(reachable=False)

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            health = await ac.get("/health")
            ready = await ac.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["provider"] == {"status": "error", "reason": "unreachable"}
    assert ready.status_code == 503
    assert ready.json()["status"] == "degraded"


async def test_provider_probe_single_flights_cold_and_expired_cache_bursts():
    """One probe request serves every concurrent caller in each cache window."""
    calls = 0

    async def request(target: str) -> None:
        nonlocal calls
        assert target == "http://fake"
        calls += 1
        await asyncio.sleep(0)

    now = 0.0
    probe = ProviderHealthProbe(request=request, monotonic=lambda: now)
    settings = SimpleNamespace(adapter_provider="ollama", ollama_base_url="http://fake")

    cold_results = await asyncio.gather(*(probe.probe(settings) for _ in range(100)))
    assert cold_results == [True] * 100
    assert calls == 1

    now = 5.1
    expired_results = await asyncio.gather(*(probe.probe(settings) for _ in range(100)))
    assert expired_results == [True] * 100
    assert calls == 2


async def test_provider_probe_applies_a_total_timeout(monkeypatch):
    """A hanging request callable returns an unreachable result within the budget."""
    monkeypatch.setattr("agents_system.main._PROVIDER_PROBE_TIMEOUT_S", 0.01)

    async def hanging_request(target: str) -> None:
        assert target == "http://fake"
        await asyncio.sleep(0.1)

    probe = ProviderHealthProbe(request=hanging_request)
    settings = SimpleNamespace(adapter_provider="ollama", ollama_base_url="http://fake")

    assert await probe.probe(settings) is False


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

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
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

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
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

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["postgres"] == "error"
    assert body["redis"] == "error"


# ---------------------------------------------------------------------------
# #141 — webhook worker / outbox backlog visibility
# ---------------------------------------------------------------------------


def _mock_healthy_deps(app) -> tuple[MagicMock, AsyncMock]:
    """Wire app.state.engine + get_redis_client so postgres/redis read ok,
    leaving the webhook-worker/backlog fields as the only variable under test.
    """
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
    return mock_engine, mock_redis


async def test_health_reports_worker_running_and_zero_backlog(app):
    """A running worker with an empty backlog is unambiguously healthy."""
    _, mock_redis = _mock_healthy_deps(app)
    fake_worker = MagicMock()
    fake_worker.is_running = True
    app.state.webhook_worker = fake_worker

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "ok"
    assert body["webhook_worker"] == {
        "running": True,
        "outbox_pending": 0,
        "outbox_leased": 0,
        "outbox_leased_expired": 0,
    }
    assert body["audit"] == {"dropped_count": 0}


async def test_health_degrades_when_worker_not_running_with_pending_backlog(app):
    """Acceptance criterion: /health reports degraded when the worker is not
    running while work is pending."""
    _, mock_redis = _mock_healthy_deps(app)
    app.state.webhook_worker = None

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=3, leased=1, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "degraded"
    assert body["webhook_worker"] == {
        "running": False,
        "outbox_pending": 3,
        "outbox_leased": 1,
        "outbox_leased_expired": 0,
    }
    assert body["audit"] == {"dropped_count": 0}


async def test_health_stays_ok_when_worker_running_despite_backlog(app):
    """A nonzero backlog alone is not degradation -- only paired with a
    worker that is not there to drain it."""
    _, mock_redis = _mock_healthy_deps(app)
    fake_worker = MagicMock()
    fake_worker.is_running = True
    app.state.webhook_worker = fake_worker

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=5, leased=2, leased_expired=2)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "ok"
    assert body["webhook_worker"]["outbox_pending"] == 5


async def test_health_stays_ok_when_worker_absent_and_backlog_empty(app):
    """No worker configured at all (e.g. WhatsApp unset) and nothing pending
    -- not every deployment runs the webhook worker."""
    _, mock_redis = _mock_healthy_deps(app)
    # app.state.webhook_worker deliberately left unset, matching a lifespan
    # that never ran (as in these tests) or a deployment with no WhatsApp.

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=0, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "ok"
    assert body["webhook_worker"]["running"] is False


async def test_health_backlog_query_failure_does_not_crash_the_endpoint(app):
    """An outbox count failure must not take /health down with it -- report
    the counts as unavailable (``None``) rather than 500 or a wrong number."""
    _, mock_redis = _mock_healthy_deps(app)
    app.state.webhook_worker = None

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(return_value=None),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["webhook_worker"]["outbox_pending"] is None
    assert body["webhook_worker"]["outbox_leased"] is None
    assert body["webhook_worker"]["outbox_leased_expired"] is None
    # Unknown backlog must not itself be treated as "pending work exists".
    assert body["status"] == "ok"


async def test_health_degrades_when_worker_not_running_with_expired_leases(app):
    """#141 review follow-up (BLOCKER): a crashed worker's abandoned,
    already-expired leases must degrade /health even when nothing is
    unleased-``pending`` -- the old rule only looked at ``outbox_pending``
    and silently ignored this exact case."""
    _, mock_redis = _mock_healthy_deps(app)
    app.state.webhook_worker = None

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=4, leased_expired=4)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "degraded"
    assert body["webhook_worker"] == {
        "running": False,
        "outbox_pending": 0,
        "outbox_leased": 4,
        "outbox_leased_expired": 4,
    }


async def test_health_stays_ok_when_worker_not_running_with_only_live_leases(app):
    """A worker not running with leased-but-not-yet-expired rows is not
    degraded on its own -- only an already-expired (abandoned) lease is,
    same as a live worker draining a nonzero ``outbox_pending``."""
    _, mock_redis = _mock_healthy_deps(app)
    app.state.webhook_worker = None

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=3, leased_expired=0)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "ok"


async def test_health_stays_ok_when_worker_running_despite_expired_leases(app):
    """A running worker will reclaim expired leases on its next poll -- an
    expired lease alone, with a live worker, is not degradation."""
    _, mock_redis = _mock_healthy_deps(app)
    fake_worker = MagicMock()
    fake_worker.is_running = True
    app.state.webhook_worker = fake_worker

    with (
        patch("agents_system.main.get_redis_client", return_value=mock_redis),
        patch(
            "agents_system.main._outbox_backlog",
            new=AsyncMock(
                return_value=OutboxBacklogCounts(pending=0, leased=1, leased_expired=1)
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get("/health")

    body = response.json()
    assert body["status"] == "ok"


async def test_outbox_backlog_probe_times_out_instead_of_hanging(app):
    """#141 review follow-up (BLOCKER): ``_outbox_backlog`` must not hang
    GET /health (and, through it, any liveness probe) when the outbox query
    itself hangs -- e.g. a locked ``outbox_work`` table. Bounded to the same
    3s budget as the postgres/redis probes; proven here against a count
    that would otherwise hang far longer than that."""
    import time

    from agents_system.main import _outbox_backlog

    class _HangingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    async def hanging_count_outbox_backlog(session):
        await asyncio.sleep(10)
        raise AssertionError(
            "count_outbox_backlog was not cancelled by the 3s health-probe timeout"
        )

    with (
        patch(
            "agents_system.main.get_session_factory",
            return_value=lambda: _HangingSession(),
        ),
        patch(
            "agents_system.main.count_outbox_backlog",
            new=hanging_count_outbox_backlog,
        ),
    ):
        start = time.monotonic()
        result = await asyncio.wait_for(_outbox_backlog(MagicMock()), timeout=8)
        elapsed = time.monotonic() - start

    assert result is None
    # Bounded by the internal 3s timeout, nowhere near the 10s hang.
    assert elapsed < 6


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

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/health")

    # At least one log line must have a request_id with 8 chars
    assert log_lines, "No log lines were captured — middleware did not log"
    request_id_lines = [line for line in log_lines if "request_id" in line]
    assert request_id_lines, "No log lines contain 'request_id'"
    for line in request_id_lines:
        assert len(line["request_id"]) == 8, (
            f"Expected 8-char request_id, got: {line['request_id']!r}"
        )


async def test_middleware_echoes_request_id_as_response_header(client):
    """#78 Phase 0 Slice 3 -- the live harness correlates an API turn's audit
    rows to the request that produced it via this header. `verify_webhook`
    needs no app.state (no engine/redis mocking), so this hits it directly;
    the header must be set on an error response too (BaseHTTPMiddleware's
    call_next still returns a Response for a raised HTTPException)."""
    response = await client.get("/webhook")

    assert response.status_code == 400
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) == 8
