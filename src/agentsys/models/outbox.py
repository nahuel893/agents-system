"""SQLAlchemy models for the durable webhook inbox and work outbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentsys.models.base import ALEMBIC_OWNED, Base


class InboundMessage(Base):
    """A Meta webhook message durably accepted before an HTTP acknowledgement."""

    __tablename__ = "webhook_inbox"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    meta_message_id: Mapped[str] = mapped_column(unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = ({"info": {ALEMBIC_OWNED: True}},)


class OutboxWork(Base):
    """A recoverable work record created atomically with its inbound message.

    W2a persists claim, retry, terminal-failure, and outbound replay state;
    it deliberately does not run a worker or call a provider.
    """

    __tablename__ = "outbox_work"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_inbox.id"),
        unique=True,
        nullable=False,
    )
    # #46 follow-up (migration 004): the sender identity a claim must
    # serialize on -- see services/outbox.py::pending_outbox_statement for
    # the per-conversation ordering this backs.
    conversation_key: Mapped[str] = mapped_column(Text, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        server_default=text("0"),
        nullable=False,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    outbound_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    outbound_send_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_outbox_work_pending",
            "available_at",
            "id",
            postgresql_where=(completed_at.is_(None) & lease_expires_at.is_(None)),
        ),
        Index(
            "ix_outbox_work_expired_lease",
            "lease_expires_at",
            "id",
            postgresql_where=(completed_at.is_(None) & lease_expires_at.is_not(None)),
        ),
        Index(
            "ix_outbox_work_ready_recoverable",
            "available_at",
            "id",
            postgresql_where=(
                completed_at.is_(None)
                & failed_at.is_(None)
                & lease_expires_at.is_(None)
            ),
        ),
        Index(
            "ix_outbox_work_expired_recoverable",
            "lease_expires_at",
            "id",
            postgresql_where=(
                completed_at.is_(None)
                & failed_at.is_(None)
                & lease_expires_at.is_not(None)
            ),
        ),
        Index(
            "ix_outbox_work_conversation_active",
            "conversation_key",
            "enqueued_at",
            "id",
            postgresql_where=(completed_at.is_(None) & failed_at.is_(None)),
        ),
        Index(
            "ux_outbox_work_conversation_live_lease",
            "conversation_key",
            unique=True,
            postgresql_where=(
                completed_at.is_(None)
                & failed_at.is_(None)
                & lease_expires_at.is_not(None)
            ),
        ),
        {"info": {ALEMBIC_OWNED: True}},
    )
