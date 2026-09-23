"""SQLAlchemy models for the durable webhook inbox and work outbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Uuid, func
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

    W1 only stores work. W2 will claim rows through ``lease_expires_at`` and
    eventually set ``completed_at`` after processing; neither workflow is
    wired here.
    """

    __tablename__ = "outbox_work"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_inbox.id"),
        unique=True,
        nullable=False,
    )
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
        {"info": {ALEMBIC_OWNED: True}},
    )
