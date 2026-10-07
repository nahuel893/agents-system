"""Proactive outbound delivery (#251, ADR-011 part 5), over the in-memory queue."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest

from agents_system.audit.sink import AuditSink
from agents_system.channels.port import (
    ProactiveChannel,
    ProviderRateLimited,
    ProviderRejected,
)
from agents_system.permissions import covers, resolve
from agents_system.proactive import (
    InMemoryProactiveQueue,
    ProactiveDispatcher,
    ProactivePermissionError,
    ProactivePolicy,
    ProactiveSender,
    RecipientNotAuthorizedError,
    StaticRecipientDirectory,
)
from agents_system.subject import TurnActor, TurnSubject

RT = "rt"
ALICE = TurnSubject.for_participant(1)
ACTOR = TurnActor(kind="scheduler", id="nightly")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class FakeChannel:
    def __init__(self, *outcomes: BaseException | None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._outcomes = list(outcomes)

    async def send_proactive(self, to: str, body: str) -> None:
        self.calls.append((to, body))
        await asyncio.sleep(0)
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if outcome is not None:
                raise outcome


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Sink:
    s = Sink()
    monkeypatch.setattr(AuditSink, "registered", classmethod(lambda cls: s))
    return s


def _directory() -> StaticRecipientDirectory:
    return StaticRecipientDirectory(
        {(RT, ALICE.key): TurnSubject(ALICE.key, channel="whatsapp", address="549111")}
    )


def _stack(
    channel: FakeChannel,
    *,
    policy: ProactivePolicy | None = None,
    directory: StaticRecipientDirectory | None = None,
    queue: InMemoryProactiveQueue | None = None,
    now: datetime = NOW,
) -> tuple[ProactiveSender, ProactiveDispatcher, InMemoryProactiveQueue]:
    queue = queue or InMemoryProactiveQueue()
    directory = directory or _directory()
    sender = ProactiveSender(
        queue, directory, deploy_grants={"send:proactive"}, clock=lambda: now
    )
    dispatcher = ProactiveDispatcher(
        queue,
        directory,
        {"whatsapp": channel},
        policy=policy or ProactivePolicy(),
        clock=lambda: now,
    )
    return sender, dispatcher, queue


async def _submit(sender: ProactiveSender, text: str = "hi") -> str:
    return await sender.submit(
        RT, ALICE, text, actor=ACTOR, permissions={"send:proactive"}
    )


def test_send_proactive_is_a_sibling_of_send_message() -> None:
    proactive, message = resolve("send:proactive"), resolve("send:message")
    assert not covers(message, proactive)
    assert not covers(proactive, message)
    assert covers(proactive, proactive)


def test_a_channel_with_send_proactive_satisfies_the_port() -> None:
    assert isinstance(FakeChannel(), ProactiveChannel)


async def test_delivers_to_the_directory_address_not_a_model_chosen_one() -> None:
    channel = FakeChannel()
    sender, dispatcher, _ = _stack(channel)
    await _submit(sender, "report ready")
    assert (await dispatcher.run_once("w1")).status == "sent"
    assert channel.calls == [("549111", "report ready")]


async def test_unauthorized_recipient_is_refused_before_any_provider_call() -> None:
    channel = FakeChannel()
    sender, _, queue = _stack(channel, directory=StaticRecipientDirectory({}))
    with pytest.raises(RecipientNotAuthorizedError):
        await _submit(sender)
    assert channel.calls == []
    assert queue.messages == []


async def test_recipient_revoked_after_enqueue_is_refused_at_dispatch(
    sink: Sink,
) -> None:
    channel = FakeChannel()
    directory = _directory()
    sender, dispatcher, _ = _stack(channel, directory=directory)
    await _submit(sender)
    directory.revoke(RT, ALICE.key)
    outcome = await dispatcher.run_once("w1")
    assert outcome.status == "failed" and outcome.reason == "recipient_not_authorized"
    assert channel.calls == []


async def test_submit_needs_the_send_proactive_permission_at_call_time() -> None:
    sender, _, queue = _stack(FakeChannel())
    for held in ({"send:message"}, set()):
        with pytest.raises(ProactivePermissionError):
            await sender.submit(RT, ALICE, "x", actor=ACTOR, permissions=held)
    assert queue.messages == []


async def test_a_grant_outside_the_deploy_ceiling_does_not_count() -> None:
    queue = InMemoryProactiveQueue()
    sender = ProactiveSender(queue, _directory(), deploy_grants=set())
    with pytest.raises(ProactivePermissionError):
        await sender.submit(RT, ALICE, "x", actor=ACTOR, permissions={"send:proactive"})


async def test_two_workers_deliver_a_message_once() -> None:
    channel = FakeChannel()
    sender, dispatcher, queue = _stack(channel)
    await _submit(sender)
    other = ProactiveDispatcher(
        queue, _directory(), {"whatsapp": channel}, clock=lambda: NOW
    )
    results = await asyncio.gather(dispatcher.run_once("w1"), other.run_once("w2"))
    assert sorted(r.status for r in results) == ["idle", "sent"]
    assert len(channel.calls) == 1


async def test_a_429_is_surfaced_with_its_delay_and_not_retried_in_a_loop() -> None:
    channel = FakeChannel(ProviderRateLimited(retry_after_s=30))
    sender, dispatcher, queue = _stack(channel)
    await _submit(sender)
    outcome = await dispatcher.run_once("w1")
    assert outcome.status == "rate_limited" and outcome.retry_after_s == 30
    # Not claimable again until the provider's delay has passed.
    assert (await dispatcher.run_once("w1")).status == "idle"
    assert len(channel.calls) == 1
    later = ProactiveDispatcher(
        queue,
        _directory(),
        {"whatsapp": channel},
        clock=lambda: NOW + timedelta(seconds=31),
    )
    assert (await later.run_once("w1")).status == "sent"
    assert len(channel.calls) == 2


async def test_an_ambiguous_failure_is_never_retried() -> None:
    channel = FakeChannel(TimeoutError("read timeout"))
    sender, dispatcher, queue = _stack(channel)
    await _submit(sender)
    assert (await dispatcher.run_once("w1")).status == "ambiguous"
    later = ProactiveDispatcher(
        queue,
        _directory(),
        {"whatsapp": channel},
        clock=lambda: NOW + timedelta(days=1),
    )
    assert (await later.run_once("w2")).status == "idle"
    assert len(channel.calls) == 1


async def test_a_definitive_rejection_is_failed_and_not_retried() -> None:
    channel = FakeChannel(ProviderRejected("bad number"))
    sender, dispatcher, _ = _stack(channel)
    await _submit(sender)
    outcome = await dispatcher.run_once("w1")
    assert outcome.status == "failed" and outcome.reason == "rejected"
    assert (await dispatcher.run_once("w1")).status == "idle"


async def test_a_worker_that_died_mid_send_leaves_an_ambiguous_message() -> None:
    channel = FakeChannel()
    sender, _, queue = _stack(channel)
    await _submit(sender)
    claimed = await queue.claim("dead-worker", now=NOW, lease_s=60)
    assert claimed is not None
    later = ProactiveDispatcher(
        queue,
        _directory(),
        {"whatsapp": channel},
        clock=lambda: NOW + timedelta(minutes=5),
    )
    assert (await later.run_once("w2")).status == "idle"
    assert channel.calls == []
    assert queue.messages[0].status == "ambiguous"


async def test_per_recipient_rate_limit_defers_instead_of_sending() -> None:
    channel = FakeChannel()
    policy = ProactivePolicy(per_recipient=(1, 3600))
    sender, dispatcher, queue = _stack(channel, policy=policy)
    await _submit(sender, "one")
    await _submit(sender, "two")
    assert (await dispatcher.run_once("w1")).status == "sent"
    deferred = await dispatcher.run_once("w1")
    assert deferred.status == "deferred" and deferred.reason == "rate_limit"
    assert [c[1] for c in channel.calls] == ["one"]
    assert queue.messages[1].status == "queued"


async def test_per_runtime_rate_limit_defers() -> None:
    channel = FakeChannel()
    policy = ProactivePolicy(per_runtime=(1, 3600))
    sender, dispatcher, _ = _stack(channel, policy=policy)
    await _submit(sender)
    await _submit(sender)
    assert (await dispatcher.run_once("w1")).status == "sent"
    assert (await dispatcher.run_once("w1")).reason == "rate_limit"


async def test_quiet_hours_hold_the_message_until_they_end() -> None:
    channel = FakeChannel()
    policy = ProactivePolicy(quiet_hours=(time(22, 0), time(8, 0)))
    night = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
    sender, dispatcher, queue = _stack(channel, policy=policy, now=night)
    await _submit(sender)
    held = await dispatcher.run_once("w1")
    assert held.status == "deferred" and held.reason == "quiet_hours"
    assert channel.calls == []
    assert queue.messages[0].available_at == datetime(2026, 10, 8, 8, 0, tzinfo=UTC)


async def test_audit_records_actor_and_subject_never_address_or_text(
    sink: Sink,
) -> None:
    sender, dispatcher, _ = _stack(FakeChannel())
    await _submit(sender, "secret body")
    await dispatcher.run_once("w1")
    events = [e for e in sink.events if e.event_type == "proactive_delivery"]
    assert [e.payload["outcome"] for e in events] == ["queued", "sent"]
    for e in events:
        assert e.actor == ACTOR.label and e.subject == ALICE.key
        assert "549111" not in str(e.payload) and "secret body" not in str(e.payload)
