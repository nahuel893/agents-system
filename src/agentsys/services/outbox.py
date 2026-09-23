"""Durable acceptance and later-worker selection for webhook outbox rows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.outbox import InboundMessage, OutboxWork

_INBOX_META_MESSAGE_CONSTRAINT = "uq_webhook_inbox_meta_message_id"


@dataclass(frozen=True)
class InboundAcceptance:
    """The committed inbound identity and whether it was already accepted."""

    inbound_message_id: uuid.UUID
    duplicate: bool


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
