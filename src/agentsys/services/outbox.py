"""Durable acceptance and later-worker selection for webhook outbox rows."""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.audit_event import AuditEvent
from agentsys.models.outbox import InboundMessage, OutboxWork

_INBOX_META_MESSAGE_CONSTRAINT = "uq_webhook_inbox_meta_message_id"
MAX_OUTBOX_ATTEMPTS = 5
DEFAULT_LEASE_DURATION = timedelta(minutes=2)
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


class OutboxLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a live, actionable outbox lease."""


async def accept_inbound_message(
    session: AsyncSession,
    *,
    meta_message_id: str,
    payload: dict[str, Any],
) -> InboundAcceptance:
    """Commit an inbound Meta message and its one work row atomically.

    A unique conflict is idempotent only when the conflicting Meta identity is
    present after rollback. Other commit failures propagate so W2's HTTP route
    can return a retryable failure instead of acknowledging an uncommitted row.
    """
    inbound = InboundMessage(
        id=uuid.uuid4(),
        meta_message_id=meta_message_id,
        payload=payload,
    )
    work = OutboxWork(
        id=uuid.uuid4(),
        inbound_message_id=inbound.id,
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
    """
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
        )
        .order_by(OutboxWork.available_at, OutboxWork.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def claim_available_outbox_work(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
) -> list[OutboxWork]:
    """Commit leases before returning work, using the database clock.

    An expired lease is recoverable. If it already consumed the maximum number
    of attempts, the lock-holder terminalizes it with its audit event instead
    of returning a sixth attempt after a crash.
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
    for work in work_rows:
        if work.attempt_count >= MAX_OUTBOX_ATTEMPTS:
            _terminalize_work(
                session,
                work=work,
                occurred_at=database_now,
                error=work.last_error
                or "lease expired after maximum delivery attempts",
            )
            continue
        work.lease_owner = worker_id
        work.lease_expires_at = database_now + lease_duration
        work.attempt_count += 1
        claimed.append(work)

    await _commit_or_rollback(session)
    return claimed


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
