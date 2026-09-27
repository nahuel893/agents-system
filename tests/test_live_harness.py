"""Offline tests for the #78 Phase 0 Slice 3 live harness helpers -- the
ones that need no real model, Postgres, or Redis (`docs/platform/live-eval.md`
"Live harness" section). The harness itself (`boot_live_harness`) is only
exercised by the opt-in `tests/test_live_smoke.py` (`pytest -m live`).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agents_system.evals.live_harness import (
    AcceptAllParticipantDirectory,
    FakeWhatsAppServer,
    LiveHarness,
    _resolve_eval_model,
    audit_rows_statement,
    build_meta_webhook_envelope,
    metric_value,
    outbox_rows_statement,
)
from agents_system.integration.meta_signature import sign_payload, verify_signature

# ---------------------------------------------------------------------------
# build_meta_webhook_envelope / sign_payload round trip
# ---------------------------------------------------------------------------


def test_build_meta_webhook_envelope_shape() -> None:
    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )

    message = envelope["entry"][0]["changes"][0]["value"]["messages"][0]
    assert message["id"] == "wamid.abc123"
    assert message["from"] == "+15550001111"
    assert message["type"] == "text"
    assert message["text"]["body"] == "hola"


def test_build_meta_webhook_envelope_round_trips_through_verify_signature() -> None:
    import json

    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )
    body = json.dumps(envelope).encode()
    secret = "a-test-secret"

    signature = sign_payload(body, secret)

    # Must not raise -- the harness signs with the exact same HMAC-SHA256
    # scheme the real `/webhook` route verifies with.
    verify_signature(body, httpx.Headers({"x-hub-signature-256": signature}), secret)


def test_build_meta_webhook_envelope_wrong_secret_is_rejected() -> None:
    import json

    from fastapi import HTTPException

    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )
    body = json.dumps(envelope).encode()
    signature = sign_payload(body, "right-secret")

    with pytest.raises(HTTPException) as exc_info:
        verify_signature(
            body, httpx.Headers({"x-hub-signature-256": signature}), "wrong-secret"
        )
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# FakeWhatsAppServer
# ---------------------------------------------------------------------------


async def test_fake_whatsapp_server_records_and_responds() -> None:
    server = FakeWhatsAppServer()
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            response = await client.post(
                "/test-phone-id/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": "+15550001111",
                    "type": "text",
                    "text": {"body": "hello from the harness"},
                },
                headers={"Authorization": "Bearer test-token"},
            )
    finally:
        await server.stop()

    assert response.status_code == 200
    assert response.json()["messages"][0]["id"] == "wamid.fake-0001"
    assert len(server.sent) == 1
    assert server.sent[0]["to"] == "+15550001111"
    assert server.sent[0]["text"]["body"] == "hello from the harness"


async def test_fake_whatsapp_server_records_every_send_in_order() -> None:
    server = FakeWhatsAppServer()
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            for index in range(3):
                await client.post(
                    "/test-phone-id/messages",
                    json={
                        "messaging_product": "whatsapp",
                        "to": f"+1555000111{index}",
                        "type": "text",
                        "text": {"body": f"message {index}"},
                    },
                )
    finally:
        await server.stop()

    assert [entry["text"]["body"] for entry in server.sent] == [
        "message 0",
        "message 1",
        "message 2",
    ]


async def test_fake_whatsapp_server_stop_swallows_non_cancellation_errors() -> None:
    """`stop()` must never let a non-cancellation exception from its own
    `serve()` task escape (PR #113 review): `boot_live_harness`'s teardown
    `finally` calls `outbound.stop()` before `_delete_redis_threads`/
    `_drop_database` -- if `stop()` re-raises (e.g. the real `serve()`
    failing with an `OSError` for a port already in use), that exception
    aborts the `finally` block before the throwaway database is dropped,
    masking the real startup error in the process. Mirrors `_drop_database`'s
    /`_delete_redis_threads`'s own never-raise teardown contract."""
    server = FakeWhatsAppServer()

    async def _boom() -> None:
        raise OSError("Address already in use")

    server._task = asyncio.create_task(_boom())

    # Must not raise.
    await server.stop()


# ---------------------------------------------------------------------------
# metric_value
# ---------------------------------------------------------------------------

_SAMPLE_METRICS_TEXT = """\
# HELP agent_turns_total Total agent turns.
# TYPE agent_turns_total counter
agent_turns_total{runtime_id="live-harness",outcome="ok"} 3.0
agent_turns_total{runtime_id="live-harness",outcome="error"} 1.0
agent_turns_total{runtime_id="other-runtime",outcome="ok"} 9.0
"""


def test_metric_value_matches_exact_labels() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="live-harness",
        outcome="ok",
    )
    assert value == 3.0


def test_metric_value_distinguishes_label_values() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="live-harness",
        outcome="error",
    )
    assert value == 1.0


def test_metric_value_returns_none_for_missing_sample() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="unknown-runtime",
        outcome="ok",
    )
    assert value is None


def test_metric_value_returns_none_for_unknown_metric_name() -> None:
    value = metric_value(_SAMPLE_METRICS_TEXT, "not_a_real_metric")
    assert value is None


# ---------------------------------------------------------------------------
# audit_rows_statement
# ---------------------------------------------------------------------------


def test_audit_rows_statement_with_no_filters_has_no_where_clause() -> None:
    stmt = audit_rows_statement()
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "WHERE" not in compiled.upper()


def test_audit_rows_statement_filters_by_correlation_id() -> None:
    stmt = audit_rows_statement(correlation_id="abcd1234")
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_event.correlation_id" in compiled
    assert "abcd1234" in compiled


def test_audit_rows_statement_combines_every_filter() -> None:
    from datetime import UTC, datetime

    stmt = audit_rows_statement(
        role="sales-agent",
        event_type="tool_call_attempted",
        tool_name="catalog_search",
        occurred_after=datetime(2026, 1, 1, tzinfo=UTC),
    )
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_event.role" in compiled
    assert "sales-agent" in compiled
    assert "audit_event.event_type" in compiled
    assert "tool_call_attempted" in compiled
    assert "audit_event.tool_name" in compiled
    assert "catalog_search" in compiled
    assert "audit_event.occurred_at" in compiled


# ---------------------------------------------------------------------------
# _run_migrations -- minimal subprocess env, no secret leakage (#113 review
# follow-up)
# ---------------------------------------------------------------------------


def test_run_migrations_does_not_copy_unrelated_secrets_into_subprocess_env(
    monkeypatch,
) -> None:
    """`_run_migrations` used to build its subprocess `env` as
    `{**os.environ, "DATABASE_URL": ...}` -- a full copy of the process
    environment, including any real model-provider secret a developer
    exported to run the live harness against a real model. It only needs
    `DATABASE_URL` (`alembic/env.py` reads that directly) plus `PATH` to
    locate the interpreter; nothing else should ever reach the child
    process's environment."""
    import subprocess
    from pathlib import Path

    from agents_system.evals import live_harness

    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-TOTALLY-REAL-LOOKING-SECRET")

    captured_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured_env.update(kwargs["env"])  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", fake_run)

    live_harness._run_migrations(
        "postgresql+asyncpg://user:pass@localhost/live_harness_test",
        repo_root=Path("/repo"),
    )

    assert "OPENAI_COMPATIBLE_API_KEY" not in captured_env
    assert captured_env["DATABASE_URL"] == (
        "postgresql+asyncpg://user:pass@localhost/live_harness_test"
    )
    assert "PATH" in captured_env


# ---------------------------------------------------------------------------
# AcceptAllParticipantDirectory
# ---------------------------------------------------------------------------


def test_accept_all_participant_directory_normalizes_address() -> None:
    directory = AcceptAllParticipantDirectory()
    assert directory.normalize_address("15550001111") == "+15550001111"
    assert directory.normalize_address("+15550001111") == "+15550001111"


def test_accept_all_participant_directory_rejects_empty_address() -> None:
    directory = AcceptAllParticipantDirectory()
    with pytest.raises(ValueError, match="empty"):
        directory.normalize_address("")


async def test_accept_all_participant_directory_resolves_to_active_participant() -> (
    None
):
    directory = AcceptAllParticipantDirectory()
    participant = await directory.resolve(session=None, address="+15550001111")
    assert participant.active is True
    assert participant.id


# ---------------------------------------------------------------------------
# LiveHarness.deliver_webhook -- used_thread_ids tracking (#113 review)
# ---------------------------------------------------------------------------


def _build_webhook_only_harness(client: httpx.AsyncClient) -> LiveHarness:
    """A `LiveHarness` with only what `deliver_webhook` itself reads set to
    something real -- `app`/`engine` are never touched by that method."""
    return LiveHarness(
        app=object(),  # type: ignore[arg-type]
        client=client,
        engine=object(),  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/0",
        outbound=FakeWhatsAppServer(),
        runtime_id="live-harness",
        model_name="fake-model",
        meta_webhook_secret="test-secret",
        metrics_api_key="test-key",
        database_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/unused",
    )


async def test_deliver_webhook_records_the_phone_as_a_used_thread_id() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def webhook(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://live-harness.test"
    ) as client:
        harness = _build_webhook_only_harness(client)
        assert harness.used_thread_ids == set()

        await harness.deliver_webhook(phone="+15550009999", text="hola")

        assert harness.used_thread_ids == {"+15550009999"}


async def test_deliver_webhook_accumulates_every_distinct_phone() -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def webhook(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://live-harness.test"
    ) as client:
        harness = _build_webhook_only_harness(client)

        await harness.deliver_webhook(phone="+15550001111", text="one")
        await harness.deliver_webhook(phone="+15550002222", text="two")
        await harness.deliver_webhook(phone="+15550001111", text="one-again")

        assert harness.used_thread_ids == {"+15550001111", "+15550002222"}


async def test_deliver_webhook_tracks_the_normalized_phone_as_the_thread_id() -> None:
    """`DeferredWebhookWorker` checkpoints under
    `AcceptAllParticipantDirectory.normalize_address(turn.phone_number)`
    (`services/webhook_worker.py`), which adds a leading `+` when the raw
    phone number is missing one. `used_thread_ids` must track that same
    normalized key -- not the raw `phone` argument -- or teardown deletes
    the wrong Redis key and leaks the real checkpoint (#113 review
    follow-up)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def webhook(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/webhook", webhook, methods=["POST"])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://live-harness.test"
    ) as client:
        harness = _build_webhook_only_harness(client)

        await harness.deliver_webhook(phone="15550009999", text="hola")

        assert harness.used_thread_ids == {"+15550009999"}


# ---------------------------------------------------------------------------
# _delete_redis_threads (#113 review, high-severity finding)
# ---------------------------------------------------------------------------


async def test_delete_redis_threads_deletes_every_tracked_thread(monkeypatch) -> None:
    import langgraph.checkpoint.redis as redis_checkpoint

    from agents_system.evals import live_harness

    deleted: list[str] = []

    class _FakeSaver:
        async def adelete_thread(self, thread_id: str) -> None:
            deleted.append(thread_id)

    class _FakeSaverContext:
        async def __aenter__(self):
            return _FakeSaver()

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class _FakeAsyncRedisSaver:
        @staticmethod
        def from_conn_string(conn_string: str) -> _FakeSaverContext:
            return _FakeSaverContext()

    monkeypatch.setattr(redis_checkpoint, "AsyncRedisSaver", _FakeAsyncRedisSaver)

    await live_harness._delete_redis_threads(
        "redis://127.0.0.1:6379/0", {"+15550001111", "+15550002222"}
    )

    assert sorted(deleted) == ["+15550001111", "+15550002222"]


async def test_delete_redis_threads_is_a_no_op_for_an_empty_set(monkeypatch) -> None:
    import langgraph.checkpoint.redis as redis_checkpoint

    from agents_system.evals import live_harness

    def _fail_from_conn_string(conn_string: str) -> None:
        raise AssertionError(
            "must not connect to Redis when there is nothing to delete"
        )

    class _FakeAsyncRedisSaver:
        from_conn_string = staticmethod(_fail_from_conn_string)

    monkeypatch.setattr(redis_checkpoint, "AsyncRedisSaver", _FakeAsyncRedisSaver)

    # Must not raise, and must not touch Redis at all.
    await live_harness._delete_redis_threads("redis://127.0.0.1:6379/0", set())


async def test_delete_redis_threads_swallows_errors_like_drop_database(
    monkeypatch,
) -> None:
    """Mirrors `_drop_database`'s never-raise contract: a teardown that fails
    must not hide the test's own result."""
    import langgraph.checkpoint.redis as redis_checkpoint

    from agents_system.evals import live_harness

    class _FakeSaverContext:
        async def __aenter__(self):
            raise ConnectionError("redis unreachable")

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class _FakeAsyncRedisSaver:
        @staticmethod
        def from_conn_string(conn_string: str) -> _FakeSaverContext:
            return _FakeSaverContext()

    monkeypatch.setattr(redis_checkpoint, "AsyncRedisSaver", _FakeAsyncRedisSaver)

    await live_harness._delete_redis_threads("redis://127.0.0.1:6379/0", {"+1555"})


# ---------------------------------------------------------------------------
# boot_live_harness -- single-boot-at-a-time serialization (#113 review,
# medium-severity finding)
# ---------------------------------------------------------------------------


async def test_boot_live_harness_never_overlaps_two_concurrent_boots(
    monkeypatch,
) -> None:
    """Two `boot_live_harness()` calls booted concurrently in the same
    process must never be "up" at the same time -- overlapping would let the
    second call's `os.environ`/`get_settings()` mutation invalidate the
    first's already-issued credentials mid-flight (this module's docstring,
    "One `boot_live_harness` at a time per process"). Every real I/O boundary
    (Postgres, the model provider, the real `create_app` lifespan) is faked;
    only the harness's own locking and its real env-mutate/restore logic are
    exercised.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from agents_system.evals import live_harness

    async def fake_create_database(admin_url: str, name: str) -> None:
        return None

    async def fake_drop_database(admin_url: str, name: str) -> None:
        return None

    def fake_run_migrations(database_url: str, *, repo_root: object) -> None:
        return None

    def fake_build_eval_model():
        return object(), "fake-model"

    def fake_create_app(**kwargs: object) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.engine = object()
            yield

        return FastAPI(lifespan=lifespan)

    monkeypatch.setattr(live_harness, "_create_database", fake_create_database)
    monkeypatch.setattr(live_harness, "_drop_database", fake_drop_database)
    monkeypatch.setattr(live_harness, "_run_migrations", fake_run_migrations)
    monkeypatch.setattr(live_harness, "build_eval_model", fake_build_eval_model)
    monkeypatch.setattr(live_harness, "create_app", fake_create_app)

    order: list[str] = []

    async def run(name: str, hold_s: float) -> None:
        async with live_harness.boot_live_harness(runtime_id=name) as harness:
            order.append(f"{name}-enter")
            assert harness.runtime_id == name
            await asyncio.sleep(hold_s)
            order.append(f"{name}-exit")

    async def run_a() -> None:
        await run("a", 0.3)

    async def run_b() -> None:
        await asyncio.sleep(0.05)
        await run("b", 0.0)

    await asyncio.gather(run_a(), run_b())

    assert order.index("a-exit") < order.index("b-enter"), (
        "b entered its own boot_live_harness() body before a's had exited -- "
        f"the two boots overlapped: {order!r}"
    )


# ---------------------------------------------------------------------------
# #119 review fixes -- boot_live_harness() permissions default, the demo
# database create/drop bracket, and _seed_demo_company()'s subprocess timeout.
# ---------------------------------------------------------------------------


async def test_boot_live_harness_requires_explicit_permissions_for_a_non_default_role(
    monkeypatch,
) -> None:
    """Omitting `permissions` must be a fail-CLOSED default, not a silent
    grant of `SALES_AGENT_PERMISSIONS` to whatever *role* happens to be
    passed (#119 review, medium finding: a fail-open default on the
    deploy-grant trust boundary this harness exists to exercise). Every real
    I/O boundary is faked (mirrors `test_boot_live_harness_never_overlaps_
    two_concurrent_boots` above) so the fixed code's `ValueError` is reached
    before any of it would even run.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from agents_system.evals import live_harness

    async def fake_create_database(admin_url: str, name: str) -> None:
        return None

    async def fake_drop_database(admin_url: str, name: str) -> None:
        return None

    def fake_run_migrations(database_url: str, *, repo_root: object) -> None:
        return None

    def fake_build_eval_model():
        return object(), "fake-model"

    captured_grants: dict[str, list[str]] = {}

    def fake_create_app(**kwargs: object) -> FastAPI:
        captured_grants.update(kwargs["grants"])  # type: ignore[arg-type]

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            app.state.engine = object()
            yield

        return FastAPI(lifespan=lifespan)

    monkeypatch.setattr(live_harness, "_create_database", fake_create_database)
    monkeypatch.setattr(live_harness, "_drop_database", fake_drop_database)
    monkeypatch.setattr(live_harness, "_run_migrations", fake_run_migrations)
    monkeypatch.setattr(live_harness, "build_eval_model", fake_build_eval_model)
    monkeypatch.setattr(live_harness, "create_app", fake_create_app)

    with pytest.raises(ValueError, match="permissions"):
        async with live_harness.boot_live_harness(
            role="operator-agent", runtime_id="probe-operator-agent"
        ):
            pass

    assert "probe-operator-agent" not in captured_grants, (
        "operator-agent must never be deploy-granted through the "
        "sales-agent default when permissions is omitted"
    )


async def test_boot_live_harness_drops_the_primary_database_when_the_demo_create_fails(
    monkeypatch,
) -> None:
    """`seed_demo=True` creates a SECOND (demo) throwaway database after the
    primary one already exists: if that second `_create_database` call
    raises, the primary database must still be dropped, never orphaned
    (#119 review, medium finding). Every real I/O boundary up to the
    simulated failure is faked; only the harness's own create/drop
    bookkeeping is exercised.
    """
    from agents_system.evals import live_harness

    created: list[str] = []
    dropped: list[str] = []

    async def fake_create_database(admin_url: str, name: str) -> None:
        created.append(name)
        if name.endswith("_demo"):
            raise RuntimeError("simulated demo database creation failure")

    async def fake_drop_database(admin_url: str, name: str) -> None:
        dropped.append(name)

    monkeypatch.setattr(live_harness, "_create_database", fake_create_database)
    monkeypatch.setattr(live_harness, "_drop_database", fake_drop_database)

    with pytest.raises(RuntimeError, match="simulated demo database creation failure"):
        async with live_harness.boot_live_harness(seed_demo=True):
            pass

    assert len(created) == 2, (
        f"expected primary + demo create attempts, got {created!r}"
    )
    assert dropped == created, (
        "every created database must be dropped when the demo create fails -- "
        f"created={created!r} dropped={dropped!r}"
    )


def test_seed_demo_company_passes_an_explicit_timeout_to_subprocess_run(
    monkeypatch,
) -> None:
    """An unreachable or misconfigured demo database must fail fast, not
    hang the whole live-eval process indefinitely (#119 review, medium
    finding) -- `subprocess.run` needs an explicit, bounded `timeout=`.
    """
    import subprocess
    from pathlib import Path

    from agents_system.evals import live_harness

    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0] if args else [], 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    live_harness._seed_demo_company("postgresql://x/y", repo_root=Path("/tmp"))

    assert isinstance(captured.get("timeout"), (int, float)), (
        f"subprocess.run was called with no bounded timeout: {captured!r}"
    )
    assert captured["timeout"] > 0


def test_seed_demo_company_raises_a_clear_error_on_timeout(monkeypatch) -> None:
    """A `subprocess.TimeoutExpired` from the demo-seeding subprocess must
    surface as a clear, distinguishing error -- not a hang, and not a bare
    `TimeoutExpired` indistinguishable from any other subprocess failure
    (#119 review, medium finding).
    """
    import subprocess
    from pathlib import Path

    from agents_system.evals import live_harness

    def fake_run(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(
            cmd="load_demo_company.py", timeout=kwargs.get("timeout", 1)
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not finish"):
        live_harness._seed_demo_company("postgresql://x/y", repo_root=Path("/tmp"))


# ---------------------------------------------------------------------------
# render_provision_sql_readonly -- Phase 1 (#79) SQL-tool fixture
# provisioning. Renders the CHECKED-IN `scripts/provision_sql_readonly.sql`
# (#80, ADR-007) into plain SQL runnable over one asyncpg connection, so a
# live Phase 1 run provisions the tool's role through the exact same runbook
# an operator follows -- see this module's docstring for why a parallel
# reimplementation would be the wrong choice.
# ---------------------------------------------------------------------------


def test_render_provision_sql_readonly_substitutes_every_psql_variable() -> None:
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="live_harness_abc123",
        password="it's-a-secret",
        views=("sql_tool_fixture.sales_v",),
    )

    # No unsubstituted psql variable or meta-command survives.
    assert ":'" not in rendered
    assert ':"' not in rendered
    assert "\\set" not in rendered
    assert "\\if" not in rendered
    assert "\\else" not in rendered
    assert "\\endif" not in rendered
    # Each substitution landed, correctly quoted/escaped.
    assert '"live_harness_abc123"' in rendered
    assert "it''s-a-secret" in rendered
    assert "sql_tool_fixture.sales_v" in rendered


def test_render_provision_sql_readonly_defaults_temp_file_limit() -> None:
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("s.v",)
    )

    assert "'256MB'" in rendered


def test_render_provision_sql_readonly_accepts_an_explicit_temp_file_limit() -> None:
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("s.v",), temp_file_limit="64MB"
    )

    assert "'64MB'" in rendered
    assert "'256MB'" not in rendered


def test_render_provision_sql_readonly_joins_multiple_views_as_one_csv_literal() -> (
    None
):
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("a.b", "c.d")
    )

    assert "'a.b,c.d'" in rendered


def test_render_provision_sql_readonly_revokes_role_membership_by_its_grantor() -> None:
    """PostgreSQL 16 tracks a role membership grant's real grantor; a bare
    `REVOKE role FROM sql_readonly` (no `GRANTED BY`) only removes what the
    CURRENT session role itself granted, leaving one granted by any other
    role in place with just a WARNING (issue #131, reproduced against a real
    PostgreSQL 16 - see `tests/test_sql_query_integration.py`'s docstring
    for how to run the live suite that exercises this end to end).
    """
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("a.b",)
    )

    assert "GRANTED BY" in rendered
    assert "grantor.rolname" in rendered


def test_render_provision_sql_readonly_strips_object_privileges_by_grantor_too() -> (
    None
):
    """A delegated (`WITH GRANT OPTION`) object privilege re-granted by a
    role other than the one running this script survives a blanket
    `REVOKE ALL ... FROM sql_readonly` too (also reproduced against a real
    PostgreSQL 16). Object-privilege `REVOKE ... GRANTED BY` only accepts
    `CURRENT_USER` (unlike role membership above), so the actual grantor is
    impersonated with `SET ROLE` instead - safe here because provisioning
    always runs as a superuser.
    """
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("a.b",)
    )

    assert "aclexplode" in rendered
    assert "SET ROLE" in rendered
    assert "RESET ROLE" in rendered


def test_render_provision_sql_readonly_raises_if_anything_remains() -> None:
    """Re-running the script is supposed to repair drift; without a check at
    the end, a REVOKE that could not match its recorded grantor committed
    anyway, so the tool's own per-call check was the first thing to notice
    (issue #131). This final check raises inside the same transaction, so
    nothing above it commits either.
    """
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("a.b",)
    )

    assert "RAISE EXCEPTION" in rendered
    assert "is still a member of role" in rendered
    assert "can still access" in rendered


def test_render_provision_sql_readonly_never_exempts_a_write_privilege_on_a_listed_view() -> (
    None
):
    """The final drift-repair check's leftover-access scan exempts exactly
    the views this run just granted SELECT on, so a normal, intended SELECT
    grant does not trip its own check. That exemption must be scoped to the
    SELECT-privilege match only: a residual WRITE privilege (INSERT/UPDATE/
    DELETE/TRUNCATE/TRIGGER/REFERENCES) surviving on the same listed view is
    exactly the kind of leftover drift this check exists to catch, and must
    never be waved through just because the view is allowlisted (issue #131
    review). In the rendered SQL, that means the exemption clause has to sit
    inside the SELECT-privilege branch - textually BEFORE the write-privilege
    checks - not after the whole three-way OR closes, which would exempt
    every branch unconditionally.
    """
    from agents_system.evals.live_harness import render_provision_sql_readonly

    rendered = render_provision_sql_readonly(
        database="db1", password="pw", views=("a.b",)
    )

    exemption_at = rendered.index("Exempt exactly the views")
    write_check_at = rendered.index("INSERT, UPDATE, REFERENCES")
    assert exemption_at < write_check_at, (
        "the view exemption must be nested inside the SELECT-privilege "
        "match only; finding it after the write-privilege checks means it "
        "exempts the whole OR, including a residual write grant on a "
        "listed view"
    )


# ---------------------------------------------------------------------------
# live_registry_factory_adapter -- `extra_tool_specs` (#79, Phase 1)
# ---------------------------------------------------------------------------


def test_live_registry_factory_adapter_extra_spec_replaces_a_same_named_base_tool() -> (
    None
):
    """A scenario that needs to inspect `run_report`'s actual arguments and
    result -- the audit trail carries neither, only name/timing/outcome --
    wraps the SAME connector and passes it as `extra_tool_specs`. This must
    REPLACE the base registry's own `run_report`, not raise
    `ToolRegistry.register`'s duplicate-name error."""
    from agents_system.evals.live_harness import live_registry_factory_adapter
    from agents_system.harness.registry import Tier, ToolSpec

    async def _fake_connector(
        inputs: dict[str, object], *, session: object = None
    ) -> dict[str, object]:
        return {"replaced": True}

    replacement = ToolSpec(
        name="run_report",
        description="replacement",
        required_permissions=(),
        input_schema={"type": "object", "properties": {}},
        connector=_fake_connector,
        tier=Tier.T0,
    )

    factory = live_registry_factory_adapter(
        object(), object(), extra_tool_specs=(replacement,)
    )
    registry = factory()

    assert registry.get("run_report") is replacement
    # Every other base tool -- unrelated to the replacement -- still comes
    # from the reference backends untouched.
    assert "catalog_search" in registry
    assert "client_lookup" in registry


def test_live_registry_factory_adapter_extra_spec_adds_a_genuinely_new_tool() -> None:
    """A tool with no base-registry counterpart (e.g. `sql_query`, #80) is
    simply added alongside every reference-backend tool."""
    from agents_system.evals.live_harness import live_registry_factory_adapter
    from agents_system.harness.registry import Tier, ToolSpec

    async def _fake_connector(
        inputs: dict[str, object], *, session: object = None
    ) -> dict[str, object]:
        return {}

    extra = ToolSpec(
        name="sql_query",
        description="fake sql_query for this test",
        required_permissions=(),
        input_schema={"type": "object", "properties": {}},
        connector=_fake_connector,
        tier=Tier.T0,
    )

    factory = live_registry_factory_adapter(
        object(), object(), extra_tool_specs=(extra,)
    )
    registry = factory()

    assert registry.get("sql_query") is extra
    assert "catalog_search" in registry


def test_live_registry_factory_adapter_tool_wrappers_replaces_with_the_wrapper_result() -> (
    None
):
    """A scenario needing `run_report`'s actual arguments/result -- unlike
    `extra_tool_specs`, which cannot pre-build a spec bound to a database
    whose name is only known once `boot_live_harness` has already booted --
    wraps whatever the base registry already built."""
    import dataclasses

    from agents_system.evals.live_harness import live_registry_factory_adapter
    from agents_system.harness.registry import ToolSpec

    calls: list[dict[str, object]] = []

    def wrap(spec: ToolSpec) -> ToolSpec:
        original = spec.connector

        async def recording(
            inputs: dict[str, object], *, session: object = None
        ) -> object:
            calls.append(dict(inputs))
            return await original(inputs, session=session)

        return dataclasses.replace(spec, connector=recording)

    factory = live_registry_factory_adapter(
        object(), object(), tool_wrappers={"run_report": wrap}
    )
    registry = factory()
    wrapped = registry.get("run_report")

    assert wrapped is not registry.get("catalog_search")
    assert wrapped.name == "run_report"
    assert wrapped.connector is not None


# ---------------------------------------------------------------------------
# _resolve_eval_model (#83, Phase 3) -- lets `boot_live_harness` accept an
# injected model (a deterministic fake for a load scenario) instead of always
# calling `build_eval_model()`, without touching any live-model call path.
# Pure/offline: no real Postgres/Redis/model needed.
# ---------------------------------------------------------------------------


def test_resolve_eval_model_returns_the_injected_model_and_name() -> None:
    fake_model = object()

    model, model_name = _resolve_eval_model(fake_model, "load-test-fake")

    assert model is fake_model
    assert model_name == "load-test-fake"


def test_resolve_eval_model_requires_a_name_when_a_model_is_given() -> None:
    with pytest.raises(ValueError, match="model_name"):
        _resolve_eval_model(object(), None)


def test_resolve_eval_model_requires_a_non_empty_name_when_a_model_is_given() -> None:
    with pytest.raises(ValueError, match="model_name"):
        _resolve_eval_model(object(), "")


def test_resolve_eval_model_falls_back_to_build_eval_model_when_both_are_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents_system.evals import live_harness

    sentinel_model = object()
    monkeypatch.setattr(
        live_harness,
        "build_eval_model",
        lambda: (sentinel_model, "real-provider-model"),
    )

    model, model_name = _resolve_eval_model(None, None)

    assert model is sentinel_model
    assert model_name == "real-provider-model"


# ---------------------------------------------------------------------------
# FakeWhatsAppServer._consume_failure (#83, Phase 3) -- lets a load scenario
# deterministically induce a bounded number of outbound-send failures for one
# recipient at a time, to exercise the outbox's retry and terminal-failure
# paths under load without touching any other concurrent conversation's
# sends. Pure/offline: never starts the real uvicorn server.
# ---------------------------------------------------------------------------


def test_fake_whatsapp_server_consume_failure_is_false_with_nothing_configured() -> (
    None
):
    server = FakeWhatsAppServer()

    assert server._consume_failure("+15550001111") is False


def test_fake_whatsapp_server_consume_failure_decrements_then_stops_failing() -> None:
    server = FakeWhatsAppServer(fail_remaining={"+15550001111": 2})

    assert server._consume_failure("+15550001111") is True
    assert server._consume_failure("+15550001111") is True
    assert server._consume_failure("+15550001111") is False
    # A second recipient configured with no entry is never affected by the
    # first's countdown.
    assert server._consume_failure("+15550002222") is False


# ---------------------------------------------------------------------------
# outbox_rows_statement (#83, Phase 3) -- a pure statement builder, unit-
# testable by compiling it, mirroring audit_rows_statement above. Lets a load
# scenario read back one conversation's outbox rows in arrival order to check
# per-conversation ordering directly against the durable rows, the same
# ordering `pending_outbox_statement` itself enforces at claim time.
# ---------------------------------------------------------------------------


def test_outbox_rows_statement_filters_by_conversation_key() -> None:
    stmt = outbox_rows_statement("+15550001111")
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "outbox_work.conversation_key" in compiled
    assert "+15550001111" in compiled


def test_outbox_rows_statement_orders_by_enqueued_at_then_id() -> None:
    stmt = outbox_rows_statement("+15550001111")
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()

    order_by_index = compiled.index("ORDER BY")
    order_clause = compiled[order_by_index:]
    assert order_clause.index("ENQUEUED_AT") < order_clause.index("OUTBOX_WORK.ID")
