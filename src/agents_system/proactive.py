"""Proactive outbound delivery (ADR-011 part 5, #251).

A channel adapter can only reply to a verified inbound turn. This module is
the other direction: a message to an authorized end user with no inbound turn
(a scheduled report, a reminder, an alert when a long job ends).

The shape, in order of what protects what:

* The recipient is a `TurnSubject`, never an address the model supplies. A
  deployment-supplied `RecipientDirectory` must authorize it, once at
  `ProactiveSender.submit` and again at dispatch (so a revoked recipient is
  refused before any provider call), and it returns the channel and address.
* The caller needs the `send:proactive` permission (T2), checked at call time
  against the deploy grant ceiling. `send:message` does not cover it.
* A message is first written to a durable `ProactiveQueue` and sent by a
  `ProactiveDispatcher` that claims it under a lease, so it goes out once
  across workers and restarts.
* Delivery is at most once, as the reply path is not: a send whose outcome is
  unknown (a timeout, a 5xx, a worker that died after claiming) is marked
  `ambiguous` and never retried. Only a provider 429, which proves nothing
  was sent, is deferred to the delay the provider named; it is not retried in
  a loop.
* Guardrails (`ProactivePolicy`): per-recipient and per-runtime rate limits
  and quiet hours defer a message rather than drop it.
* Audit records who triggered it and for which subject, never the address or
  the text.

This module has no dependency on the rest of the package beyond the subject
types, the permission registry and the audit recorder.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal, Protocol, runtime_checkable

import structlog

from agents_system.permissions import UnknownPermissionNameError, covers, resolve
from agents_system.subject import TurnActor, TurnSubject

logger = structlog.get_logger(__name__)

SEND_PROACTIVE = "send:proactive"
DEFAULT_LEASE_S = 120
DEFAULT_RECHECK_S = 60

MessageStatus = Literal["queued", "sending", "sent", "failed", "ambiguous"]
#: Statuses that count against a rate limit: the provider may have been reached.
ATTEMPTED_STATUSES: frozenset[str] = frozenset({"sending", "sent", "ambiguous"})


class ProviderRateLimited(Exception):
    """The provider answered 429: nothing was sent; try again after ``retry_after_s``."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"provider rate limit; retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class ProviderRejected(Exception):
    """The provider definitively refused the message (a 4xx other than 429)."""


class RecipientNotAuthorizedError(Exception):
    """The deployment's directory does not authorize this subject."""


class ProactivePermissionError(Exception):
    """The caller does not hold ``send:proactive`` within the deploy grants."""


@runtime_checkable
class ProactiveChannel(Protocol):
    """The proactive half of a channel adapter.

    Raises `ProviderRateLimited` on a 429, `ProviderRejected` on a definitive
    refusal and anything else when the outcome is unknown.
    """

    async def send_proactive(self, to: str, body: str) -> None: ...


@runtime_checkable
class RecipientDirectory(Protocol):
    """The deployment's allowlist of who may be messaged unprompted."""

    async def authorize(
        self, runtime_id: str, subject: TurnSubject
    ) -> TurnSubject | None:
        """``subject`` with its channel and address, or ``None`` when not authorized."""
        ...


class StaticRecipientDirectory:
    """Reference directory over a dict keyed by (runtime id, subject key)."""

    def __init__(self, recipients: Mapping[tuple[str, str], TurnSubject]) -> None:
        self._recipients = dict(recipients)

    def revoke(self, runtime_id: str, subject_key: str) -> None:
        self._recipients.pop((runtime_id, subject_key), None)

    async def authorize(
        self, runtime_id: str, subject: TurnSubject
    ) -> TurnSubject | None:
        found = self._recipients.get((runtime_id, subject.key))
        if found is None or not found.channel or not found.address:
            return None
        return found


@dataclass(frozen=True)
class ProactiveMessage:
    """One queued message. It holds no address: that comes from the directory."""

    id: str
    runtime_id: str
    subject_key: str
    text: str
    actor: str
    status: MessageStatus = "queued"
    available_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None


@runtime_checkable
class ProactiveQueue(Protocol):
    """Durable queue. ``claim`` hands a message to exactly one worker."""

    async def enqueue(
        self,
        runtime_id: str,
        subject_key: str,
        text: str,
        *,
        actor: str,
        available_at: datetime,
    ) -> str: ...

    async def claim(
        self, worker_id: str, *, now: datetime, lease_s: float
    ) -> ProactiveMessage | None:
        """Mark one due message ``sending`` for this worker.

        A message left ``sending`` past its lease was claimed by a worker that
        may have reached the provider; it becomes ``ambiguous``, never queued.
        """
        ...

    async def attempts_since(
        self,
        runtime_id: str,
        subject_key: str | None,
        since: datetime,
        *,
        exclude: str | None = None,
    ) -> int:
        """Messages the provider may have been reached for, since ``since``."""
        ...

    async def finish(
        self, message_id: str, worker_id: str, status: MessageStatus, error: str | None
    ) -> None: ...

    async def defer(self, message_id: str, worker_id: str, until: datetime) -> None:
        """Return a claimed message to ``queued``, due at ``until``."""
        ...


class InMemoryProactiveQueue:
    """Reference queue for library mode and tests (one event loop, no persistence)."""

    def __init__(self) -> None:
        self.messages: list[ProactiveMessage] = []
        self._times: dict[str, datetime] = {}

    def _replace(self, message_id: str, **changes: object) -> None:
        self.messages = [
            replace(m, **changes) if m.id == message_id else m  # type: ignore[arg-type]
            for m in self.messages
        ]

    async def enqueue(
        self,
        runtime_id: str,
        subject_key: str,
        text: str,
        *,
        actor: str,
        available_at: datetime,
    ) -> str:
        message = ProactiveMessage(
            id=str(uuid.uuid4()),
            runtime_id=runtime_id,
            subject_key=subject_key,
            text=text,
            actor=actor,
            available_at=available_at,
        )
        self.messages.append(message)
        return message.id

    async def claim(
        self, worker_id: str, *, now: datetime, lease_s: float
    ) -> ProactiveMessage | None:
        for m in self.messages:
            if (
                m.status == "sending"
                and m.lease_expires_at is not None
                and m.lease_expires_at <= now
            ):
                self._replace(m.id, status="ambiguous", last_error="lease_expired")
        due = [
            m
            for m in self.messages
            if m.status == "queued" and (m.available_at or now) <= now
        ]
        if not due:
            return None
        chosen = min(due, key=lambda m: m.available_at or now)
        self._replace(
            chosen.id,
            status="sending",
            lease_owner=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_s),
        )
        self._times[chosen.id] = now
        return next(m for m in self.messages if m.id == chosen.id)

    async def attempts_since(
        self,
        runtime_id: str,
        subject_key: str | None,
        since: datetime,
        *,
        exclude: str | None = None,
    ) -> int:
        return sum(
            1
            for m in self.messages
            if m.runtime_id == runtime_id
            and (subject_key is None or m.subject_key == subject_key)
            and m.status in ATTEMPTED_STATUSES
            and m.id != exclude
            and self._times.get(m.id, since) >= since
        )

    async def finish(
        self, message_id: str, worker_id: str, status: MessageStatus, error: str | None
    ) -> None:
        self._replace(
            message_id,
            status=status,
            last_error=error,
            lease_owner=None,
            lease_expires_at=None,
        )

    async def defer(self, message_id: str, worker_id: str, until: datetime) -> None:
        self._replace(
            message_id,
            status="queued",
            available_at=until,
            lease_owner=None,
            lease_expires_at=None,
        )
        self._times.pop(message_id, None)


@dataclass(frozen=True)
class ProactivePolicy:
    """Guardrails. A limit is ``(count, window_seconds)``; ``None`` is unlimited.

    ``quiet_hours`` is ``(start, end)`` in ``tz`` and may wrap midnight.
    """

    per_recipient: tuple[int, int] | None = None
    per_runtime: tuple[int, int] | None = None
    quiet_hours: tuple[time, time] | None = None
    tz: tzinfo = UTC
    recheck_s: float = DEFAULT_RECHECK_S

    def quiet_until(self, now: datetime) -> datetime | None:
        """When quiet hours end, if ``now`` falls inside them."""
        if self.quiet_hours is None:
            return None
        start, end = self.quiet_hours
        local = now.astimezone(self.tz)
        t = local.time()
        inside = (start <= t < end) if start <= end else (t >= start or t < end)
        if not inside:
            return None
        ends = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if ends <= local:
            ends += timedelta(days=1)
        return ends.astimezone(now.tzinfo or UTC)


@dataclass(frozen=True)
class DispatchOutcome:
    """What one `run_once` did. ``idle`` means nothing was due."""

    status: Literal["idle", "sent", "failed", "ambiguous", "rate_limited", "deferred"]
    reason: str | None = None
    retry_after_s: float | None = None
    message_id: str | None = None


async def _audit(
    outcome: str,
    *,
    runtime_id: str,
    subject_key: str,
    actor: str,
    message_id: str,
    channel: str | None = None,
    reason: str | None = None,
) -> None:
    """Record a delivery event; auditing must never change delivery."""
    from agents_system.audit.recorder import record_proactive_delivery
    from agents_system.audit.sink import AuditSink

    sink = AuditSink.registered()
    if sink is None:
        return
    try:
        await sink.record(
            await record_proactive_delivery(
                outcome=outcome,
                runtime_id=runtime_id,
                subject_key=subject_key,
                actor=actor,
                message_id=message_id,
                channel=channel,
                reason=reason,
            )
        )
    except Exception:
        logger.warning("proactive.audit_failed", exc_info=True)


class ProactiveSender:
    """The entry point callers use: authorize, check the grant, enqueue."""

    def __init__(
        self,
        queue: ProactiveQueue,
        directory: RecipientDirectory,
        *,
        deploy_grants: Collection[str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._directory = directory
        self._ceiling = _classes(deploy_grants)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def submit(
        self,
        runtime_id: str,
        subject: TurnSubject,
        text: str,
        *,
        actor: TurnActor,
        permissions: Collection[str],
        send_at: datetime | None = None,
    ) -> str:
        """Queue ``text`` for ``subject``; returns the message id.

        Raises `ProactivePermissionError` unless ``permissions`` (the caller's
        current grants) are covered by the deploy ceiling for ``send:proactive``,
        and `RecipientNotAuthorizedError` when the directory refuses the
        subject. Neither reaches the queue or a provider.
        """
        required = resolve(SEND_PROACTIVE)
        effective = self._ceiling & _classes(permissions)
        if not any(covers(g, required) for g in effective):
            raise ProactivePermissionError(
                f"{SEND_PROACTIVE} is not granted to {actor.label}"
            )
        if await self._directory.authorize(runtime_id, subject) is None:
            raise RecipientNotAuthorizedError(
                f"subject {subject.key!r} is not an authorized proactive recipient"
            )
        message_id = await self._queue.enqueue(
            runtime_id,
            subject.key,
            text,
            actor=actor.label,
            available_at=send_at or self._clock(),
        )
        await _audit(
            "queued",
            runtime_id=runtime_id,
            subject_key=subject.key,
            actor=actor.label,
            message_id=message_id,
        )
        return message_id


def _classes(names: Collection[str]) -> set[type]:
    found: set[type] = set()
    for name in names:
        try:
            found.add(resolve(name))
        except UnknownPermissionNameError:
            continue
    return found


class ProactiveDispatcher:
    """Claim one due message and send it, at most once."""

    def __init__(
        self,
        queue: ProactiveQueue,
        directory: RecipientDirectory,
        channels: Mapping[str, ProactiveChannel],
        *,
        policy: ProactivePolicy | None = None,
        lease_s: float = DEFAULT_LEASE_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queue = queue
        self._directory = directory
        self._channels = dict(channels)
        self._policy = policy or ProactivePolicy()
        self._lease_s = lease_s
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, worker_id: str) -> DispatchOutcome:
        now = self._clock()
        message = await self._queue.claim(worker_id, now=now, lease_s=self._lease_s)
        if message is None:
            return DispatchOutcome("idle")
        # A worker cancelled mid-send leaves the message ``sending``: the
        # provider may have been reached, so the lease expiry marks it
        # ``ambiguous`` and it is never requeued.
        return await self._dispatch(worker_id, message, now)

    async def _dispatch(
        self, worker_id: str, message: ProactiveMessage, now: datetime
    ) -> DispatchOutcome:
        ident = {
            "runtime_id": message.runtime_id,
            "subject_key": message.subject_key,
            "actor": message.actor,
            "message_id": message.id,
        }
        recipient = await self._directory.authorize(
            message.runtime_id, TurnSubject(message.subject_key)
        )
        if recipient is None or not recipient.channel or not recipient.address:
            return await self._finish(
                worker_id, message, "failed", "recipient_not_authorized", ident
            )
        channel = self._channels.get(recipient.channel)
        if channel is None:
            return await self._finish(
                worker_id, message, "failed", "no_channel", ident, recipient.channel
            )

        held = await self._guard(worker_id, message, now, ident, recipient.channel)
        if held is not None:
            return held

        try:
            await channel.send_proactive(recipient.address, message.text)
        except ProviderRateLimited as limited:
            until = now + timedelta(seconds=limited.retry_after_s)
            await self._queue.defer(message.id, worker_id, until)
            await _audit("rate_limited", channel=recipient.channel, **ident)
            return DispatchOutcome(
                "rate_limited",
                reason="provider_429",
                retry_after_s=limited.retry_after_s,
                message_id=message.id,
            )
        except ProviderRejected:
            return await self._finish(
                worker_id, message, "failed", "rejected", ident, recipient.channel
            )
        except Exception:
            logger.warning("proactive.send_ambiguous", exc_info=True)
            return await self._finish(
                worker_id, message, "ambiguous", "send_error", ident, recipient.channel
            )
        return await self._finish(
            worker_id, message, "sent", None, ident, recipient.channel
        )

    async def _guard(
        self,
        worker_id: str,
        message: ProactiveMessage,
        now: datetime,
        ident: dict[str, str],
        channel: str,
    ) -> DispatchOutcome | None:
        policy = self._policy
        until = policy.quiet_until(now)
        reason = "quiet_hours"
        if until is None:
            reason = "rate_limit"
            for limit, subject_key in (
                (policy.per_recipient, message.subject_key),
                (policy.per_runtime, None),
            ):
                if limit is None:
                    continue
                count, window_s = limit
                used = await self._queue.attempts_since(
                    message.runtime_id,
                    subject_key,
                    now - timedelta(seconds=window_s),
                    exclude=message.id,
                )
                if used >= count:
                    until = now + timedelta(seconds=policy.recheck_s)
                    break
        if until is None:
            return None
        await self._queue.defer(message.id, worker_id, until)
        await _audit("deferred", channel=channel, reason=reason, **ident)
        return DispatchOutcome("deferred", reason=reason, message_id=message.id)

    async def _finish(
        self,
        worker_id: str,
        message: ProactiveMessage,
        status: MessageStatus,
        reason: str | None,
        ident: dict[str, str],
        channel: str | None = None,
    ) -> DispatchOutcome:
        await self._queue.finish(message.id, worker_id, status, reason)
        await _audit(status, channel=channel, reason=reason, **ident)
        return DispatchOutcome(
            status,  # type: ignore[arg-type]
            reason=reason,
            message_id=message.id,
        )
