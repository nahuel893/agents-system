"""Durable acceptance and later-worker selection for webhook outbox rows."""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, exists, func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agents_system.models.audit_event import AuditEvent
from agents_system.models.outbox import InboundMessage, OutboxWork

_INBOX_META_MESSAGE_CONSTRAINT = "uq_webhook_inbox_meta_message_id"
MAX_OUTBOX_ATTEMPTS = 5
# The runtime permits turns below 300 seconds; keep a bounded lease longer than
# one worst-case turn plus provider send, rather than recovering live work early.
DEFAULT_LEASE_DURATION = timedelta(minutes=10)
_RETRY_BASE_SECONDS = 1
_RETRY_MAX_SECONDS = 300


@dataclass(frozen=True)
class InboundAcceptance:
    """The committed inbound identity and whether it was already accepted."""

    inbound_message_id: uuid.UUID
    duplicate: bool


@dataclass(frozen=True)
class OutboxFailureOutcome:
    """The persisted failure state that W2b must act on after commit.

    ``alert_required`` is a durable operator-action signal, not evidence that
    a human notifier has run. W2b owns any deployment-specific escalation.
    """

    terminal: bool
    alert_required: bool


@dataclass(frozen=True)
class OutboxClaimOutcome:
    """Committed claim results, including terminal recovery transitions."""

    claimed: list[OutboxWork]
    terminalized: list[OutboxWork]


@dataclass(frozen=True)
class OutboxBacklogCounts:
    """Non-terminal ``outbox_work`` counts, split by lease state (#141).

    ``pending`` counts rows no worker has claimed yet (``lease_expires_at``
    is ``NULL``), including any still in retry backoff -- it deliberately
    does not require ``available_at <= now`` the way
    ``pending_outbox_statement`` does, because this is a backlog-depth
    signal for ``GET /health``, not a claim candidate list. ``leased``
    counts rows with a lease, whether still live or expired and awaiting
    reclaim by ``claim_available_outbox_work`` -- a crashed worker's
    abandoned lease is still "leased" until something reclaims it.
    ``leased_expired`` is the subset of ``leased`` whose
    ``lease_expires_at`` is already in the past against the database clock
    (#141 review follow-up) -- rows a crashed worker abandoned mid-flight,
    recoverable only once some worker claims them again. A live (not yet
    expired) lease held by an actively running worker is ``leased`` but not
    ``leased_expired``.

    A completed or failed row is terminal and counted in neither bucket.
    """

    pending: int
    leased: int
    leased_expired: int = 0


class OutboxLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a live, actionable outbox lease."""


async def accept_inbound_message(
    session: AsyncSession,
    *,
    meta_message_id: str,
    payload: dict[str, Any],
    conversation_key: str,
) -> InboundAcceptance:
    """Commit an inbound Meta message and its one work row atomically.

    A unique conflict is idempotent only when the conflicting Meta identity is
    present after rollback. Other commit failures propagate so W2's HTTP route
    can return a retryable failure instead of acknowledging an uncommitted row.

    *conversation_key* (#46 follow-up) is the caller's choice of what must
    serialize: two rows sharing one key are never both claimable at once (see
    ``pending_outbox_statement``). This module stays payload-format agnostic
    -- it does not interpret *payload* itself -- so the caller (the webhook
    route) resolves the key from the Meta envelope before calling this.
    """
    if not conversation_key:
        raise ValueError("conversation_key must not be empty")

    inbound = InboundMessage(
        id=uuid.uuid4(),
        meta_message_id=meta_message_id,
        payload=payload,
    )
    work = OutboxWork(
        id=uuid.uuid4(),
        inbound_message_id=inbound.id,
        conversation_key=conversation_key,
    )
    session.add_all([inbound, work])

    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if _constraint_name(error) != _INBOX_META_MESSAGE_CONSTRAINT:
            raise
        existing = await session.scalar(
            select(InboundMessage).where(
                InboundMessage.meta_message_id == meta_message_id
            )
        )
        if existing is not None:
            return InboundAcceptance(
                inbound_message_id=existing.id,
                duplicate=True,
            )
        raise
    except Exception:
        await session.rollback()
        raise

    return InboundAcceptance(inbound_message_id=inbound.id, duplicate=False)


def _constraint_name(error: IntegrityError) -> str | None:
    """Return the PostgreSQL constraint name carried by a DBAPI error."""
    dbapi_error = error.orig
    if dbapi_error is None:
        return None
    for original in (dbapi_error, dbapi_error.__cause__):
        if original is None:
            continue
        name = getattr(
            original,
            "constraint_name",
            getattr(getattr(original, "diag", None), "constraint_name", None),
        )
        if isinstance(name, str):
            return name
    return None


def pending_outbox_statement(
    *,
    now: datetime,
    limit: int,
) -> Select[tuple[OutboxWork]]:
    """Select work that W2 may lease, without claiming or processing it.

    The two partial indexes on ``outbox_work`` cover unleased ready work and
    expired leases. ``SKIP LOCKED`` lets future workers select independently;
    it does not itself create a lease or send a provider request.

    #46 follow-up -- per-conversation ordering: a row is excluded whenever an
    older (by arrival order: ``enqueued_at``, then ``id``) non-terminal
    sibling still exists for the same ``conversation_key``, regardless of
    that sibling's own lease or backoff state. This is what makes the whole
    conversation wait behind its oldest item -- live-leased, still in retry
    backoff, or simply unclaimed -- without a separate lease-liveness check:
    once that oldest sibling terminalizes (completed or failed), it drops out
    of the subquery and the next message of that conversation becomes
    claimable. Combined with row-level ``FOR UPDATE SKIP LOCKED`` on the
    single resulting candidate per conversation, two workers cannot both
    claim one conversation; ``ux_outbox_work_conversation_live_lease``
    (migration 004) is a DB-level backstop for that same invariant, not the
    primary mechanism.
    """
    sibling = aliased(OutboxWork)
    no_older_sibling = ~exists(
        select(1).where(
            sibling.conversation_key == OutboxWork.conversation_key,
            sibling.completed_at.is_(None),
            sibling.failed_at.is_(None),
            tuple_(sibling.enqueued_at, sibling.id)
            < tuple_(OutboxWork.enqueued_at, OutboxWork.id),
        )
    )
    return (
        select(OutboxWork)
        .where(
            OutboxWork.completed_at.is_(None),
            OutboxWork.failed_at.is_(None),
            OutboxWork.available_at <= now,
            or_(
                OutboxWork.lease_expires_at.is_(None),
                OutboxWork.lease_expires_at < now,
            ),
            no_older_sibling,
        )
        .order_by(OutboxWork.available_at, OutboxWork.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def count_outbox_backlog(session: AsyncSession) -> OutboxBacklogCounts:
    """Count non-terminal ``outbox_work`` rows, split by lease state (#141).

    Two separate ``COUNT(*)`` queries rather than one grouped query: the
    ``NULL``/``NOT NULL`` split on ``lease_expires_at`` already matches
    ``ix_outbox_work_ready_recoverable`` and ``ix_outbox_work_expired_recoverable``
    (see ``models/outbox.py``), so each count hits its own partial index
    instead of a full-table scan plus in-memory grouping. A third query
    narrows the ``leased`` bucket to already-expired leases
    (``leased_expired``, #141 review follow-up) -- still a range scan on
    ``ix_outbox_work_expired_recoverable`` (indexed on ``lease_expires_at``),
    now bounded by the database clock rather than just ``IS NOT NULL``.
    Comparing against the database clock (not app-server wall time) matches
    every other lease comparison in this module (``claim_available_outbox_work``
    etc. via ``_database_now``), so a skewed app clock cannot mis-classify a
    lease that is, from the database's own point of view, still live.

    Read-only and uses no row locks (unlike ``pending_outbox_statement``,
    this never claims anything) -- safe to call from ``GET /health`` on
    every request.
    """
    not_terminal = (OutboxWork.completed_at.is_(None), OutboxWork.failed_at.is_(None))

    pending = await session.scalar(
        select(func.count())
        .select_from(OutboxWork)
        .where(*not_terminal, OutboxWork.lease_expires_at.is_(None))
    )
    leased = await session.scalar(
        select(func.count())
        .select_from(OutboxWork)
        .where(*not_terminal, OutboxWork.lease_expires_at.is_not(None))
    )
    database_now = await _database_now(session)
    leased_expired = await session.scalar(
        select(func.count())
        .select_from(OutboxWork)
        .where(
            *not_terminal,
            OutboxWork.lease_expires_at.is_not(None),
            OutboxWork.lease_expires_at < database_now,
        )
    )
    return OutboxBacklogCounts(
        pending=_as_count(pending),
        leased=_as_count(leased),
        leased_expired=_as_count(leased_expired),
    )


def _as_count(value: Any) -> int:
    """Coerce one ``COUNT(*)`` scalar result to ``int``.

    Against a real database this is always ``NULL`` or an integer. Rejecting
    anything else (rather than laundering it through ``value or 0``) matters
    because a caller (``GET /health``, #141) does arithmetic and comparisons
    on this value -- silently accepting an unexpected truthy object here
    would surface as a wrong health signal instead of a loud failure.
    """
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    raise TypeError(f"expected COUNT(*) to return an int or None, got {value!r}")


async def claim_available_outbox_work(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
) -> OutboxClaimOutcome:
    """Commit leases and terminal recovery state using the database clock.

    An expired lease is recoverable. If it already consumed the maximum number
    of attempts, the lock-holder terminalizes it with its audit event instead
    of returning a sixth attempt after a crash. The returned terminalized rows
    are committed durable operator-action signals for W2b's notifier.
    """
    if not worker_id:
        raise ValueError("worker_id must not be empty")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if lease_duration <= timedelta():
        raise ValueError("lease_duration must be positive")

    database_now = await _database_now(session)
    work_rows = list(
        (
            await session.scalars(
                pending_outbox_statement(now=database_now, limit=limit)
            )
        ).all()
    )
    claimed: list[OutboxWork] = []
    terminalized: list[OutboxWork] = []
    for work in work_rows:
        if work.attempt_count >= MAX_OUTBOX_ATTEMPTS:
            _terminalize_work(
                session,
                work=work,
                occurred_at=database_now,
                error=work.last_error
                or "lease expired after maximum delivery attempts",
            )
            terminalized.append(work)
            continue
        work.lease_owner = worker_id
        work.lease_expires_at = database_now + lease_duration
        work.attempt_count += 1
        claimed.append(work)

    await _commit_or_rollback(session)
    return OutboxClaimOutcome(claimed=claimed, terminalized=terminalized)


async def persist_outbound_intent(
    session: AsyncSession,
    *,
    work: OutboxWork,
    worker_id: str,
    body: dict[str, Any],
    send_key: str,
) -> None:
    """Commit a reply only while this worker still owns a live DB-time lease."""
    if not send_key:
        raise ValueError("send_key must not be empty")

    current = await _locked_live_work(session, work_id=work.id, worker_id=worker_id)
    if current.outbound_send_key is not None and current.outbound_send_key != send_key:
        await session.rollback()
        raise ValueError("outbound intent already has a different send_key")
    if current.outbound_body is not None and current.outbound_body != body:
        await session.rollback()
        raise ValueError("outbound intent already has a different body")

    current.outbound_body = body
    current.outbound_send_key = send_key
    await _commit_or_rollback(session)


async def complete_outbox_work(
    session: AsyncSession,
    *,
    work: OutboxWork,
    worker_id: str,
) -> None:
    """Fence successful or deliberately non-service completion to a live lease."""
    database_now = await _database_now(session)
    current = await _locked_live_work(session, work_id=work.id, worker_id=worker_id)
    current.completed_at = database_now
    current.lease_owner = None
    current.lease_expires_at = None
    await _commit_or_rollback(session)


async def record_outbox_failure(
    session: AsyncSession,
    *,
    work: OutboxWork,
    worker_id: str,
    error: str,
    jitter: Callable[[], float] = random.random,
) -> OutboxFailureOutcome:
    """Fence a failure transition to its live lease using database time."""
    database_now = await _database_now(session)
    current = await _locked_live_work(session, work_id=work.id, worker_id=worker_id)
    current.last_error = error
    current.lease_owner = None
    current.lease_expires_at = None

    if current.attempt_count >= MAX_OUTBOX_ATTEMPTS:
        _terminalize_work(
            session,
            work=current,
            occurred_at=database_now,
            error=error,
        )
        await _commit_or_rollback(session)
        return OutboxFailureOutcome(terminal=True, alert_required=True)

    retry_window_seconds = min(
        _RETRY_BASE_SECONDS * (2**current.attempt_count),
        _RETRY_MAX_SECONDS,
    )
    jitter_value = jitter()
    if not 0 <= jitter_value <= 1:
        await session.rollback()
        raise ValueError("jitter must return a value between 0 and 1")
    current.available_at = database_now + timedelta(
        seconds=retry_window_seconds * jitter_value
    )
    await _commit_or_rollback(session)
    return OutboxFailureOutcome(terminal=False, alert_required=False)


async def _database_now(session: AsyncSession) -> datetime:
    """Return PostgreSQL's wall-clock time for lease and retry transitions."""
    database_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime):
        raise TypeError("database did not return a timestamp")
    return database_now


async def _locked_live_work(
    session: AsyncSession,
    *,
    work_id: uuid.UUID,
    worker_id: str,
) -> OutboxWork:
    """Lock the current row only if its lease is still owned and actionable."""
    current = await session.scalar(
        select(OutboxWork)
        .where(
            OutboxWork.id == work_id,
            OutboxWork.lease_owner == worker_id,
            OutboxWork.lease_expires_at > func.clock_timestamp(),
            OutboxWork.completed_at.is_(None),
            OutboxWork.failed_at.is_(None),
        )
        .with_for_update()
    )
    if current is None:
        await session.rollback()
        raise OutboxLeaseLostError("outbox lease is no longer owned or live")
    return current


def _terminalize_work(
    session: AsyncSession,
    *,
    work: OutboxWork,
    occurred_at: datetime,
    error: str,
) -> None:
    """Make terminal failure and its durable operator-action event atomic."""
    work.last_error = error
    work.lease_owner = None
    work.lease_expires_at = None
    work.failed_at = occurred_at
    session.add(
        AuditEvent(
            event_id=uuid.uuid4(),
            occurred_at=occurred_at,
            correlation_id=f"outbox:{work.id}",
            sequence=work.attempt_count,
            event_type="webhook_delivery_terminal_failure",
            payload={
                "error": error,
                "attempt_count": work.attempt_count,
                "outbox_work_id": str(work.id),
                "operator_action": "required",
            },
        )
    )


async def _commit_or_rollback(session: AsyncSession) -> None:
    """Commit a state transition or leave no uncommitted state on failure."""
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
