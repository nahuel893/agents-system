"""#290 -- the deferred worker retries transient turn failures only, and
tells the end user once when a turn fails for good.

The outbox here is a small stateful stand-in for the database: ``claim``
leases the one row again each poll (the backoff delay is the database's, so
a fake clock needs no waiting) and ``record_outbox_failure`` is the real one,
so the attempt count, the terminal transition and the fallback intent are the
production ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog.testing
from langchain_core.messages import AIMessage
from prometheus_client import CollectorRegistry

from agents_system.models.audit_event import AuditEvent
from agents_system.models.outbox import InboundMessage, OutboxWork
from agents_system.services.outbox import OutboxClaimOutcome
from agents_system.services.outbox_metrics import build_outbox_failure_counter
from agents_system.services.retry_policy import (
    DEFAULT_FALLBACK_REPLY,
    OutboxRetryPolicy,
)
from agents_system.services.webhook_worker import ChannelBinding, DeferredWebhookWorker

NOW = datetime(2026, 8, 4, tzinfo=UTC)
USER = "+5491123456789"


class _Db:
    """One row's database: the session every worker call opens."""

    def __init__(self, inbound: InboundMessage, work: OutboxWork) -> None:
        self.inbound = inbound
        self.work = work
        self.audit: list[AuditEvent] = []
        self.commit = AsyncMock()

    def __call__(self) -> _Db:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, model: object, identity: object) -> InboundMessage:
        return self.inbound

    async def scalar(self, statement: object) -> Any:
        return self.work if "FROM outbox_work" in str(statement) else NOW

    def add(self, row: object) -> None:
        if isinstance(row, AuditEvent):
            self.audit.append(row)

    async def rollback(self) -> None:
        return None

    def events(self) -> list[str]:
        return [row.event_type for row in self.audit]


class _Directory:
    def __init__(self) -> None:
        self.normalize_address = MagicMock(return_value=USER)
        participant = MagicMock(id=7, active=True)
        self.resolve = AsyncMock(return_value=participant)


def _inbound() -> InboundMessage:
    return InboundMessage(
        id=uuid4(),
        meta_message_id="wamid.1",
        payload={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.1",
                                        "from": "5491123456789",
                                        "text": {"body": "hello"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )


@pytest.fixture
def row() -> tuple[_Db, OutboxWork]:
    inbound = _inbound()
    work = OutboxWork(id=uuid4(), inbound_message_id=inbound.id, attempt_count=0)
    return _Db(inbound, work), work


@pytest.fixture(autouse=True)
def _outbox(monkeypatch: pytest.MonkeyPatch, row: tuple[_Db, OutboxWork]) -> None:
    _, work = row

    async def claim(
        session: object, *, worker_id: str, limit: int, **kwargs: Any
    ) -> OutboxClaimOutcome:
        if work.failed_at is not None or work.completed_at is not None:
            return OutboxClaimOutcome(claimed=[], terminalized=[])
        work.attempt_count += 1
        work.lease_owner = worker_id
        work.lease_expires_at = NOW + timedelta(minutes=10)
        return OutboxClaimOutcome(claimed=[work], terminalized=[])

    monkeypatch.setattr(
        "agents_system.services.webhook_worker.claim_available_outbox_work", claim
    )


def _worker(
    db: _Db,
    *,
    turn: Any,
    send: Any | None = None,
    policy: OutboxRetryPolicy | None = None,
    counter: Any | None = None,
) -> tuple[DeferredWebhookWorker, MagicMock]:
    runtime = MagicMock()
    runtime.run_turn = turn
    channel = MagicMock()
    channel.send_text = send if send is not None else AsyncMock()
    from agents_system.channels.whatsapp import WhatsAppChannel

    adapter = WhatsAppChannel(sender=channel)
    kwargs: dict[str, Any] = {}
    if counter is not None:
        kwargs["failure_counter"] = counter
    worker = DeferredWebhookWorker(
        session_factory=db,
        worker_id="worker-a",
        directory=_Directory(),
        channels={
            "whatsapp": ChannelBinding(
                adapter=adapter,
                runtime=runtime,
                retry_policy=policy or OutboxRetryPolicy(),
            )
        },
        **kwargs,
    )
    return worker, channel


async def _poll(worker: DeferredWebhookWorker, times: int = 12) -> None:
    for _ in range(times):
        await worker.process_available(limit=1)


async def test_a_deterministic_turn_failure_is_attempted_once_not_five_times(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    turn = AsyncMock(side_effect=ValueError("tool crashed on this input"))
    worker, _ = _worker(db, turn=turn)

    await _poll(worker)

    assert turn.await_count == 1
    assert work.attempt_count == 1
    assert work.failed_at is not None


async def test_a_transient_provider_error_is_still_retried_with_backoff(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    calls = 0

    async def turn(**kwargs: Any) -> list[Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("provider timed out")
        return [AIMessage(content="all good")]

    send = AsyncMock()
    worker, _ = _worker(db, turn=turn, send=send)

    await _poll(worker)

    assert calls == 3
    assert work.completed_at is not None
    assert work.failed_at is None
    send.assert_awaited_once_with(to=USER, body="all good")
    # each retry was scheduled in the future by the real backoff
    assert work.available_at is not None
    assert "webhook_fallback_reply" not in db.events()


async def test_transient_errors_stop_at_the_max_attempts_then_tell_the_user(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    turn = AsyncMock(side_effect=ConnectionError("network down"))
    send = AsyncMock()
    worker, _ = _worker(db, turn=turn, send=send)

    await _poll(worker)

    assert turn.await_count == 5
    assert work.failed_at is not None
    send.assert_awaited_once_with(to=USER, body=DEFAULT_FALLBACK_REPLY)


async def test_the_user_gets_the_fallback_exactly_once_even_when_polled_again(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    send = AsyncMock()
    worker, _ = _worker(db, turn=AsyncMock(side_effect=ValueError("bad")), send=send)

    await _poll(worker, times=3)
    # a later claim of the same row (e.g. a stale poll) finds nothing to do
    work.lease_owner = None
    await _poll(worker, times=3)

    send.assert_awaited_once_with(to=USER, body=DEFAULT_FALLBACK_REPLY)
    assert db.events() == [
        "webhook_delivery_terminal_failure",
        "webhook_fallback_reply",
    ]
    assert db.audit[1].payload["delivered"] is True
    assert work.outbound_send_key == "wamid.1:fallback"


async def test_a_failed_fallback_send_is_audited_and_never_retried(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, _ = row
    send = AsyncMock(side_effect=RuntimeError("provider down"))
    worker, _ = _worker(db, turn=AsyncMock(side_effect=ValueError("bad")), send=send)

    await _poll(worker)

    assert send.await_count == 1
    assert db.audit[-1].event_type == "webhook_fallback_reply"
    assert db.audit[-1].payload["delivered"] is False


async def test_no_fallback_when_a_reply_was_already_committed(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    work.outbound_body = {"to": USER, "body": "the real reply"}
    work.outbound_send_key = "wamid.1:reply"
    turn = AsyncMock()
    send = AsyncMock(side_effect=ValueError("provider rejected the message"))
    worker, _ = _worker(db, turn=turn, send=send)

    await _poll(worker)

    turn.assert_not_awaited()
    assert work.failed_at is not None
    send.assert_awaited_once_with(to=USER, body="the real reply")
    assert work.outbound_body == {"to": USER, "body": "the real reply"}


async def test_a_timed_out_turn_reply_is_a_normal_reply_not_a_failure(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, work = row
    timeout_reply = "This is taking longer than expected. Please try again."
    turn = AsyncMock(return_value=[AIMessage(content=timeout_reply)])
    send = AsyncMock()
    worker, _ = _worker(db, turn=turn, send=send)

    await _poll(worker)

    send.assert_awaited_once_with(to=USER, body=timeout_reply)
    assert work.completed_at is not None
    assert db.events() == []


async def test_the_policy_sets_the_cap_the_classifier_and_the_reply(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, _ = row
    policy = OutboxRetryPolicy(
        max_attempts=2,
        is_transient=lambda exc: isinstance(exc, ValueError),
        fallback_reply="Un momento, algo salio mal. {not_a_template}",
    )
    turn = AsyncMock(side_effect=ValueError("now retryable"))
    send = AsyncMock()
    worker, _ = _worker(db, turn=turn, send=send, policy=policy)

    await _poll(worker)

    assert turn.await_count == 2
    send.assert_awaited_once_with(
        to=USER, body="Un momento, algo salio mal. {not_a_template}"
    )


async def test_a_permanent_failure_is_logged_and_counted_by_class_only(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, _ = row
    registry = CollectorRegistry()
    counter = build_outbox_failure_counter(registry)
    secret = "customer 5491123456789 asked about card 4111"
    worker, _ = _worker(
        db, turn=AsyncMock(side_effect=ValueError(secret)), counter=counter
    )

    with structlog.testing.capture_logs() as logs:
        await _poll(worker, times=2)

    sample = registry.get_sample_value(
        "outbox_permanent_failures_total",
        {"channel": "whatsapp", "exception": "ValueError", "reason": "permanent"},
    )
    assert sample == 1.0
    event = next(e for e in logs if e["event"] == "webhook_worker.permanent_failure")
    assert event["exception_class"] == "ValueError"
    assert event["reason"] == "permanent"
    assert secret not in repr(logs[logs.index(event)])
    for family in registry.collect():
        for metric in family.samples:
            assert secret not in repr(metric)


async def test_exhausted_retries_are_counted_as_such(
    row: tuple[_Db, OutboxWork],
) -> None:
    db, _ = row
    registry = CollectorRegistry()
    counter = build_outbox_failure_counter(registry)
    worker, _ = _worker(
        db, turn=AsyncMock(side_effect=TimeoutError("slow")), counter=counter
    )

    await _poll(worker)

    assert (
        registry.get_sample_value(
            "outbox_permanent_failures_total",
            {
                "channel": "whatsapp",
                "exception": "TimeoutError",
                "reason": "retries_exhausted",
            },
        )
        == 1.0
    )


async def test_an_infrastructure_condition_the_worker_reports_is_always_retried(
    monkeypatch: pytest.MonkeyPatch, row: tuple[_Db, OutboxWork]
) -> None:
    db, _ = row
    record = AsyncMock(
        return_value=MagicMock(terminal=False, alert_required=False, fallback_body=None)
    )
    monkeypatch.setattr(
        "agents_system.services.webhook_worker.record_outbox_failure", record
    )
    worker = DeferredWebhookWorker(
        session_factory=db,
        worker_id="worker-a",
        directory=None,
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
    )

    await worker.process_available(limit=1)

    record.assert_awaited_once()
    assert record.await_args is not None
    assert record.await_args.kwargs["retryable"] is True
