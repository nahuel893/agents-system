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
    audit_rows_statement,
    build_meta_webhook_envelope,
    metric_value,
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
