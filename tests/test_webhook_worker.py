"""Direct W2b1 tests for the deferred durable webhook processor."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog.testing
from langchain_core.messages import AIMessage

from agentsys.models.outbox import InboundMessage, OutboxWork
from agentsys.services.admission import TurnAdmissionLimiter
from agentsys.services.outbox import (
    OutboxClaimOutcome,
    OutboxFailureOutcome,
    OutboxLeaseLostError,
)
from agentsys.services.webhook_worker import DeferredWebhookWorker


class _Session:
    def __init__(self, inbound: InboundMessage | None) -> None:
        self.inbound = inbound
        self.commit = AsyncMock()

    async def get(self, model: object, identity: object) -> InboundMessage | None:
        assert model is InboundMessage
        return self.inbound


class _SessionFactory:
    def __init__(self, inbound: InboundMessage | None) -> None:
        self.inbound = inbound
        self.sessions: list[_Session] = []

    def __call__(self) -> _SessionContext:
        session = _Session(self.inbound)
        self.sessions.append(session)
        return _SessionContext(session)


class _SessionContext:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> _Session:
        return self.session

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        return None


class _Participant:
    def __init__(self, *, active: bool = True) -> None:
        self.id = 7
        self.active = active


class _Directory:
    def __init__(self, participant: _Participant | None) -> None:
        self.normalize_address = MagicMock(return_value="+5491123456789")
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


def _work(inbound: InboundMessage) -> OutboxWork:
    return OutboxWork(id=uuid4(), inbound_message_id=inbound.id, attempt_count=1)


def _install_claim(
    monkeypatch: pytest.MonkeyPatch,
    work: OutboxWork,
) -> None:
    async def claim(*args: object, **kwargs: object) -> OutboxClaimOutcome:
        return OutboxClaimOutcome(claimed=[work], terminalized=[])

    monkeypatch.setattr(
        "agentsys.services.webhook_worker.claim_available_outbox_work", claim
    )


async def test_processor_persists_reply_before_at_least_once_provider_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    persisted: list[tuple[dict[str, Any], str]] = []
    completed: list[OutboxWork] = []
    events: list[str] = []

    async def persist(
        session: object,
        *,
        work: OutboxWork,
        worker_id: str,
        body: dict[str, Any],
        send_key: str,
    ) -> None:
        persisted.append((body, send_key))
        events.append("persisted")

    async def complete(
        session: object,
        *,
        work: OutboxWork,
        worker_id: str,
    ) -> None:
        completed.append(work)
        events.append("completed")

    async def send(*, to: str, body: str) -> None:
        events.append("sent")

    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persist
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", complete
    )

    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock(side_effect=send)
    factory = _SessionFactory(inbound)

    worker = DeferredWebhookWorker(
        session_factory=factory,
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
        checkpointer_enabled=True,
    )

    await worker.process_available(limit=1)

    assert persisted == [({"to": "+5491123456789", "body": "reply"}, "wamid.1:reply")]
    runtime.run_turn.assert_awaited_once()
    assert runtime.run_turn.await_args.kwargs["session_id"] == "wamid.1"
    assert runtime.run_turn.await_args.kwargs["thread_id"] == "+5491123456789"
    whatsapp.send_text.assert_awaited_once_with(to="+5491123456789", body="reply")
    assert completed == [work]
    assert events == ["persisted", "sent", "completed"]


async def test_ambiguous_send_reuses_persisted_reply_without_rerunning_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    work.outbound_body = {"to": "+5491123456789", "body": "persisted reply"}
    work.outbound_send_key = "wamid.1:reply"
    _install_claim(monkeypatch, work)

    async def complete(*args: object, **kwargs: object) -> None:
        return None

    persist = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persist
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", complete
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock()
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_available(limit=1)

    runtime.run_turn.assert_not_awaited()
    persist.assert_not_awaited()
    whatsapp.send_text.assert_awaited_once_with(
        to="+5491123456789", body="persisted reply"
    )


async def test_non_service_directory_outcomes_complete_without_turn_or_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", completed
    )

    for directory in (_Directory(None), _Directory(_Participant(active=False))):
        inbound = _inbound()
        _install_claim(monkeypatch, _work(inbound))
        runtime = MagicMock()
        runtime.run_turn = AsyncMock()
        whatsapp = MagicMock()
        whatsapp.send_text = AsyncMock()
        worker = DeferredWebhookWorker(
            session_factory=_SessionFactory(inbound),
            worker_id="worker-a",
            directory=directory,
            runtime=runtime,
            whatsapp_client=whatsapp,
        )

        await worker.process_available(limit=1)

        runtime.run_turn.assert_not_awaited()
        whatsapp.send_text.assert_not_awaited()

    assert completed.await_count == 2


async def test_missing_directory_retries_instead_of_discarding_accepted_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    _install_claim(monkeypatch, _work(inbound))
    failure = AsyncMock(return_value=OutboxFailureOutcome(False, False))
    completion = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", failure
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", completion
    )
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=None,
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
    )

    await worker.process_available(limit=1)

    failure.assert_awaited_once()
    assert failure.await_args is not None
    assert "participant directory" in failure.await_args.kwargs["error"]
    completion.assert_not_awaited()


async def test_normalize_failure_retries_unless_it_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    _install_claim(monkeypatch, _work(inbound))
    failure = AsyncMock(return_value=OutboxFailureOutcome(False, False))
    completion = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", failure
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", completion
    )
    directory = _Directory(_Participant())
    directory.normalize_address.side_effect = RuntimeError("directory unavailable")
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=directory,
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
    )

    await worker.process_available(limit=1)

    failure.assert_awaited_once()
    completion.assert_not_awaited()

    failure.reset_mock()
    directory.normalize_address.side_effect = ValueError("bad address")
    await worker.process_available(limit=1)

    failure.assert_not_awaited()
    completion.assert_awaited_once()


async def test_turn_failure_records_retry_and_notifies_only_after_terminal_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    committed: list[str] = []

    async def record_failure(*args: object, **kwargs: object) -> OutboxFailureOutcome:
        committed.append("terminal state committed")
        return OutboxFailureOutcome(terminal=True, alert_required=True)

    notifier = AsyncMock(side_effect=lambda work, error: committed.append("notified"))
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(side_effect=RuntimeError("runtime unavailable"))

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=MagicMock(),
        terminal_notifier=notifier,
    )

    await worker.process_available(limit=1)

    assert committed == ["terminal state committed", "notified"]
    notifier.assert_awaited_once()


async def test_committed_terminal_claim_notifies_after_the_claim_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = _work(_inbound())
    work.last_error = "lease expired after maximum delivery attempts"
    events: list[str] = []

    async def claim(*args: object, **kwargs: object) -> OutboxClaimOutcome:
        events.append("claim committed")
        return OutboxClaimOutcome(claimed=[], terminalized=[work])

    notifier = AsyncMock(side_effect=lambda work, error: events.append("notified"))
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.claim_available_outbox_work", claim
    )
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-a",
        directory=None,
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        terminal_notifier=notifier,
    )

    await worker.process_available(limit=1)

    assert events == ["claim committed", "notified"]
    notifier.assert_awaited_once_with(work, work.last_error)


async def test_provider_failure_records_a_retry_after_persisting_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    persisted = AsyncMock()
    record_failure = AsyncMock(return_value=OutboxFailureOutcome(False, False))
    completed = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persisted
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", completed
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock(side_effect=RuntimeError("provider timeout"))

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_available(limit=1)

    persisted.assert_awaited_once()
    record_failure.assert_awaited_once()
    completed.assert_not_awaited()


async def test_checkpointer_thread_id_is_omitted_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", AsyncMock()
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="")])

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=MagicMock(),
        checkpointer_enabled=False,
    )

    await worker.process_available(limit=1)

    assert runtime.run_turn.await_args.kwargs["thread_id"] is None


async def test_shared_envelope_selects_the_inbound_meta_id_not_the_first_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.first",
                                    "from": "5491111111111",
                                    "text": {"body": "first text"},
                                },
                                {
                                    "id": "wamid.second",
                                    "from": "5491222222222",
                                    "text": {"body": "second text"},
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    }
    first = InboundMessage(
        id=uuid4(), meta_message_id="wamid.first", payload=shared_payload
    )
    second = InboundMessage(
        id=uuid4(), meta_message_id="wamid.second", payload=shared_payload
    )
    persisted: list[dict[str, Any]] = []

    async def persist(
        session: object,
        *,
        work: OutboxWork,
        worker_id: str,
        body: dict[str, Any],
        send_key: str,
    ) -> None:
        persisted.append(body)

    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persist
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", AsyncMock()
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(
        side_effect=[
            [AIMessage(content="reply one")],
            [AIMessage(content="reply two")],
        ]
    )
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()
    directory = _Directory(_Participant())
    directory.normalize_address.side_effect = lambda raw: f"+{raw}"
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(first),
        worker_id="worker-a",
        directory=directory,
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_claimed_work(_work(first))
    second_worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(second),
        worker_id="worker-a",
        directory=directory,
        runtime=runtime,
        whatsapp_client=whatsapp,
    )
    await second_worker.process_claimed_work(_work(second))

    assert [
        call.kwargs["messages"][0].content for call in runtime.run_turn.await_args_list
    ] == ["first text", "second text"]
    assert [call.kwargs["session_id"] for call in runtime.run_turn.await_args_list] == [
        "wamid.first",
        "wamid.second",
    ]
    assert persisted == [
        {"to": "+5491111111111", "body": "reply one"},
        {"to": "+5491222222222", "body": "reply two"},
    ]


async def test_shared_envelope_missing_inbound_meta_id_is_non_serviceable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = InboundMessage(
        id=uuid4(),
        meta_message_id="wamid.absent",
        payload={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.other",
                                        "from": "5491111111111",
                                        "text": {"body": "other text"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )
    completed = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", completed
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock()
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_claimed_work(_work(inbound))

    completed.assert_awaited_once()
    runtime.run_turn.assert_not_awaited()
    whatsapp.send_text.assert_not_awaited()


async def test_runtime_and_send_cancellation_propagate_without_failure_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    record_failure = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(side_effect=asyncio.CancelledError())
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=MagicMock(),
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.process_claimed_work(_work(inbound))

    replay_work = _work(inbound)
    replay_work.outbound_body = {"to": "+5491123456789", "body": "reply"}
    replay_work.outbound_send_key = "wamid.1:reply"
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock(side_effect=asyncio.CancelledError())
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=whatsapp,
    )

    with pytest.raises(asyncio.CancelledError):
        await worker.process_claimed_work(replay_work)

    record_failure.assert_not_awaited()


async def test_lease_loss_stops_without_stale_failure_or_provider_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)

    async def persist(*args: object, **kwargs: object) -> None:
        raise OutboxLeaseLostError("lost")

    record_failure = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persist
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_available(limit=1)

    record_failure.assert_not_awaited()
    whatsapp.send_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# W2b2 — start/stop poll loop lifecycle
# ---------------------------------------------------------------------------


async def test_start_schedules_a_poll_loop_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start()`` runs ``process_available`` on the configured interval, and
    calling it twice does not spawn a second loop."""
    calls: list[int] = []

    async def process_available(self: DeferredWebhookWorker, *, limit: int) -> None:
        calls.append(limit)

    monkeypatch.setattr(DeferredWebhookWorker, "process_available", process_available)
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-loop",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        poll_interval_s=0.01,
        claim_limit=3,
    )

    await worker.start()
    await worker.start()  # idempotent — must not spawn a second loop task
    await asyncio.sleep(0.05)
    await worker.stop()

    assert calls, "the poll loop never called process_available"
    assert calls[0] == 3


async def test_stop_cancels_an_in_flight_iteration_without_a_failure_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop()`` hard-cancels the loop instead of waiting it out. A claim
    left mid-flight must not be reported as failed — its lease simply expires
    and becomes reclaimable, the same recovery path a mid-turn cancellation
    already relies on (W2b1)."""
    started = asyncio.Event()
    record_failure = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )

    async def process_available(self: DeferredWebhookWorker, *, limit: int) -> None:
        started.set()
        await asyncio.sleep(10)  # would hang the test if stop() did not cancel it

    monkeypatch.setattr(DeferredWebhookWorker, "process_available", process_available)
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-loop",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        poll_interval_s=0.01,
    )

    await worker.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(worker.stop(), timeout=1)

    record_failure.assert_not_awaited()


async def test_stop_before_start_is_a_no_op() -> None:
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-loop",
        directory=None,
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
    )

    await worker.stop()  # must not raise


async def test_is_running_reflects_start_stop_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#141 — GET /health reports whether the worker is running; ``is_running``
    is the signal it reads, and must track ``start``/``stop`` exactly."""

    async def process_available(self: DeferredWebhookWorker, *, limit: int) -> None:
        pass

    monkeypatch.setattr(DeferredWebhookWorker, "process_available", process_available)
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-loop",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        poll_interval_s=0.01,
    )

    assert worker.is_running is False

    await worker.start()
    assert worker.is_running is True

    await worker.stop()
    assert worker.is_running is False


async def test_is_running_reads_false_after_the_loop_task_dies_uncaught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#141 review follow-up (BLOCKER) -- ``_run_loop`` only catches
    ``Exception`` per iteration; a bug that raises a bare ``BaseException``
    (or anything else escaping that catch) kills the loop task without ever
    going through ``stop()``, leaving ``_started`` stuck at ``True`` with
    nothing actually polling. ``is_running`` must catch this itself by also
    checking the loop task's own liveness, not just the start/stop flag."""

    class _FatalBug(BaseException):
        pass

    async def process_available(self: DeferredWebhookWorker, *, limit: int) -> None:
        raise _FatalBug("unrecoverable bug escaping the poll loop")

    monkeypatch.setattr(DeferredWebhookWorker, "process_available", process_available)
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-loop",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        poll_interval_s=0.01,
    )

    await worker.start()
    assert worker.is_running is True

    task = worker._loop_task
    assert task is not None
    with pytest.raises(_FatalBug):
        await asyncio.wait_for(task, timeout=1)

    # The task is dead, but nothing ever called stop() -- _started alone
    # would still (wrongly) say the worker is running.
    assert worker._started is True
    assert worker.is_running is False


# ---------------------------------------------------------------------------
# W2b2 — ported from the retired webhook-route test (route no longer runs a
# turn at all; the worker is now the only caller of ``run_turn``)
# ---------------------------------------------------------------------------


async def test_process_claimed_work_does_not_force_empty_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (discovery #184): a write:/send: tool call executes through
    the worker's ``run_turn`` call identically to the adapter entry point --
    the worker must not force an empty permissions tuple. Previously this
    lived in ``tests/test_webhook.py`` against the synchronous route; W2b2
    retires that route's own ``run_turn`` call, so the guard moves here."""
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    from agentsys.agent.graph import AgentRuntime
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.loader import AgentDefinition
    from agentsys.harness.registry import Tier, ToolSpec

    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", AsyncMock()
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", AsyncMock()
    )

    invoked: list[dict[str, Any]] = []

    def create_order(inputs: dict[str, Any]) -> dict[str, Any]:
        invoked.append(inputs)
        return {"order_id": "ord-002", "status": "created"}

    order_spec = ToolSpec(
        name="create_order",
        required_permissions=("write:orders",),
        connector=create_order,
        tier=Tier.T2,
        description="Create an order",
        input_schema={"type": "object", "properties": {}},
    )
    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=("write:orders",),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(order_spec,),
        denied_tools=(),
        skills=(),
    )

    class _ToolAwareFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            return self

    first_response = AIMessage(
        content="",
        tool_calls=[
            {"id": "call_1", "name": "create_order", "args": {}, "type": "tool_call"}
        ],
    )
    final_response = AIMessage(content="Order created.")
    model = _ToolAwareFakeModel(responses=[first_response, final_response])
    agent = AgentRuntime(equipped, model)

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=agent,
        whatsapp_client=MagicMock(send_text=AsyncMock()),
    )

    await worker.process_available(limit=1)

    # If the worker forced permissions=(), the interceptor would raise
    # PolicyViolation before the connector ever ran — invoked would stay empty.
    assert len(invoked) == 1


# ---------------------------------------------------------------------------
# Post-review follow-ups — directory failure, recorder success/failure,
# empty-reply completion, and explicit non-text-message handling
# ---------------------------------------------------------------------------


async def test_directory_resolve_failure_records_a_retry_without_running_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``directory.resolve`` raising (a DB error, say) must go through
    ``record_outbox_failure`` -- retried/eventually failed, never silently
    dropped -- and must never reach ``run_turn``."""
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    record_failure = AsyncMock(return_value=OutboxFailureOutcome(False, False))
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )

    class _RaisingDirectory:
        def normalize_address(self, raw: str) -> str:
            return "+5491123456789"

        async def resolve(self, session: object, address: str) -> object:
            raise RuntimeError("directory lookup exploded")

    runtime = MagicMock()
    runtime.run_turn = AsyncMock()
    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_RaisingDirectory(),
        runtime=runtime,
        whatsapp_client=MagicMock(),
    )

    await worker.process_available(limit=1)

    record_failure.assert_awaited_once()
    assert record_failure.call_args.kwargs["work"] is work
    runtime.run_turn.assert_not_awaited()


async def test_recorder_present_records_the_turn_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured recorder's ``record_turn`` is called with the completed
    turn once a reply is produced."""
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", AsyncMock()
    )
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", AsyncMock()
    )

    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    recorded: list[dict[str, Any]] = []

    class _Recorder:
        async def record_turn(
            self,
            session: object,
            *,
            thread_id: str,
            participant_id: Any,
            user_text: str,
            assistant_text: str,
        ) -> None:
            recorded.append(
                {
                    "thread_id": thread_id,
                    "participant_id": participant_id,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                }
            )

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
        recorder=_Recorder(),
    )

    await worker.process_available(limit=1)

    assert recorded == [
        {
            "thread_id": "+5491123456789",
            "participant_id": 7,
            "user_text": "hello",
            "assistant_text": "reply",
        }
    ]


async def test_recorder_failure_logs_a_warning_and_the_turn_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder that raises must not block the reply or the completion --
    conversation recording is best effort -- but the failure must be visible
    as a structured warning, not silently swallowed."""
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", AsyncMock()
    )
    complete = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", complete
    )

    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    class _RaisingRecorder:
        async def record_turn(self, session: object, **kwargs: object) -> None:
            raise RuntimeError("recorder DB down")

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
        recorder=_RaisingRecorder(),
    )

    with structlog.testing.capture_logs() as logs:
        await worker.process_available(limit=1)

    warnings = [e for e in logs if e["event"] == "webhook_worker.recorder_failed"]
    assert len(warnings) == 1
    whatsapp.send_text.assert_awaited_once()
    complete.assert_awaited_once()


async def test_empty_reply_completes_the_work_instead_of_leaving_it_dangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty extracted reply must still terminate the claim through
    ``complete_outbox_work`` -- not send, not persist a reply, and not leave
    the row claimable forever."""
    inbound = _inbound()
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    complete = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", complete
    )
    persist = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.persist_outbound_intent", persist
    )

    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="")])
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    await worker.process_available(limit=1)

    complete.assert_awaited_once()
    assert complete.call_args.kwargs["work"] is work
    whatsapp.send_text.assert_not_awaited()
    persist.assert_not_awaited()


async def test_non_text_message_is_durably_completed_without_a_turn_or_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image/audio/location message is accepted and completed like any
    other non-serviceable inbound content -- no turn, no reply -- and its
    Meta ``type`` is explicitly logged. Replying to non-text messages is a
    documented product follow-up, not something this worker attempts."""
    inbound = InboundMessage(
        id=uuid4(),
        meta_message_id="wamid.image-1",
        payload={
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.image-1",
                                        "from": "5491123456789",
                                        "type": "image",
                                        "image": {
                                            "id": "media-1",
                                            "mime_type": "image/jpeg",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        },
    )
    work = _work(inbound)
    _install_claim(monkeypatch, work)
    complete = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.complete_outbox_work", complete
    )

    runtime = MagicMock()
    runtime.run_turn = AsyncMock()
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )

    with structlog.testing.capture_logs() as logs:
        await worker.process_available(limit=1)

    runtime.run_turn.assert_not_awaited()
    whatsapp.send_text.assert_not_awaited()
    complete.assert_awaited_once()
    non_text_logs = [e for e in logs if e["event"] == "webhook_worker.non_text_message"]
    assert len(non_text_logs) == 1
    assert non_text_logs[0]["message_type"] == "image"


# ---------------------------------------------------------------------------
# #46 — bounded concurrent turns (admission control)
# ---------------------------------------------------------------------------


async def test_process_available_claims_nothing_when_admission_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) at capacity, additional work must not be claimed at all: no lease
    is taken and no attempt is spent on a row the worker cannot run yet."""
    claim = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.claim_available_outbox_work", claim
    )
    limiter = TurnAdmissionLimiter(1)
    await limiter.acquire()  # saturate the only slot

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        admission_limiter=limiter,
    )

    await worker.process_available(limit=5)

    claim.assert_not_awaited()


async def test_process_available_resumes_claiming_once_a_slot_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) continued: once admission has room again, claiming resumes, and
    the amount claimed is capped at the slots actually free -- not at
    ``limit``."""
    inbound = _inbound()
    claim_calls: list[int] = []

    async def claim(*args: object, **kwargs: object) -> OutboxClaimOutcome:
        claim_calls.append(kwargs["limit"])
        return OutboxClaimOutcome(claimed=[], terminalized=[])

    monkeypatch.setattr(
        "agentsys.services.webhook_worker.claim_available_outbox_work", claim
    )
    limiter = TurnAdmissionLimiter(1)
    await limiter.acquire()

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        admission_limiter=limiter,
    )

    await worker.process_available(limit=5)
    assert claim_calls == [], "claimed durable work while at capacity"

    await limiter.release()
    await worker.process_available(limit=5)
    assert claim_calls == [1], "must cap the claim at the single free slot"


async def test_process_available_claims_only_the_reserved_slot_count_and_runs_it_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) mutation-provable, updated for SHOULD-FIX 1 (reserve admission
    BEFORE claiming): with max_concurrent_turns=2 and 5 ready DB rows,
    process_available claims exactly 2 -- the reserved count, never the
    raw ``limit`` -- and runs both concurrently (no artificial
    serialization on top of what was already reserved). Deterministic via
    events -- no sleeps as a substitute for synchronization."""
    inbound = _inbound()
    works = [_work(inbound) for _ in range(5)]
    requested_limits: list[int] = []

    async def claim(*args: object, **kwargs: object) -> OutboxClaimOutcome:
        requested = cast(int, kwargs["limit"])
        requested_limits.append(requested)
        return OutboxClaimOutcome(claimed=list(works[:requested]), terminalized=[])

    monkeypatch.setattr(
        "agentsys.services.webhook_worker.claim_available_outbox_work", claim
    )

    in_flight = 0
    peak = 0
    entered = [asyncio.Event() for _ in range(2)]
    release = asyncio.Event()

    async def fake_process(self: DeferredWebhookWorker, work: OutboxWork) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        entered[works.index(work)].set()
        await release.wait()
        in_flight -= 1

    monkeypatch.setattr(DeferredWebhookWorker, "process_claimed_work", fake_process)

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(inbound),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        admission_limiter=TurnAdmissionLimiter(2),
    )

    task = asyncio.create_task(worker.process_available(limit=5))
    try:
        await asyncio.wait_for(
            asyncio.gather(entered[0].wait(), entered[1].wait()), timeout=1
        )
        assert requested_limits == [2], (
            "the claim must be capped at the reserved slot count (2), "
            "not the requested limit (5)"
        )
        assert peak == 2, "both reserved items must run concurrently"

        release.set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()

    assert peak == 2


async def test_process_claimed_work_admitted_never_waits_for_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHOULD-FIX 1: a claimed row's slot was already reserved in
    process_available -- _process_claimed_work_admitted must never itself
    block acquiring one (that would tick the claimed row's lease down while
    waiting on a slot admission already promised was free). Proven by
    making the limiter's acquire()/slot() raise if ever called from here;
    only release() may be used."""
    limiter = TurnAdmissionLimiter(1)
    assert limiter.try_acquire() is True  # simulate process_available's reservation

    async def forbidden_acquire() -> None:
        raise AssertionError("_process_claimed_work_admitted must not wait for a slot")

    monkeypatch.setattr(limiter, "acquire", forbidden_acquire)

    async def fake_process(self: DeferredWebhookWorker, work: OutboxWork) -> None:
        return None

    monkeypatch.setattr(DeferredWebhookWorker, "process_claimed_work", fake_process)

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        admission_limiter=limiter,
    )
    work = _work(_inbound())

    await worker._process_claimed_work_admitted(work)

    assert limiter.in_flight == 0, "the reserved slot must be released after processing"


async def test_process_claimed_work_admitted_releases_slot_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SHOULD-FIX 4: a claimed item cancelled mid-flight (e.g. worker
    shutdown) still releases its reserved admission slot -- and, matching
    the existing mid-turn-cancellation guarantee, records no spurious
    failure, so the lease simply expires and becomes reclaimable."""
    limiter = TurnAdmissionLimiter(1)
    assert limiter.try_acquire() is True

    record_failure = AsyncMock()
    monkeypatch.setattr(
        "agentsys.services.webhook_worker.record_outbox_failure", record_failure
    )

    started = asyncio.Event()
    hang_forever = asyncio.Event()

    async def fake_process(self: DeferredWebhookWorker, work: OutboxWork) -> None:
        started.set()
        await hang_forever.wait()

    monkeypatch.setattr(DeferredWebhookWorker, "process_claimed_work", fake_process)

    worker = DeferredWebhookWorker(
        session_factory=_SessionFactory(None),
        worker_id="worker-a",
        directory=_Directory(_Participant()),
        runtime=MagicMock(),
        whatsapp_client=MagicMock(),
        admission_limiter=limiter,
    )
    work = _work(_inbound())

    task = asyncio.create_task(worker._process_claimed_work_admitted(work))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.in_flight == 0, "the reserved slot must be released on cancellation"
    record_failure.assert_not_awaited()
