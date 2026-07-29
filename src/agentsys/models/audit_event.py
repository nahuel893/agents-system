"""SQLAlchemy 2.0 async model for audit_event — partitioned audit log."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentsys.models.base import Base


class AuditEvent(Base):
    """Partitioned audit event table.

    Partitioned by RANGE (occurred_at) monthly.
    Columns match REQ-AUDIT-41 from the D-007 spec.

    PostgreSQL requires all PRIMARY KEY and UNIQUE constraints on a
    partitioned table to include all partition key columns.
    Therefore:
    - PRIMARY KEY is (occurred_at, id)
    - UNIQUE is (occurred_at, correlation_id, sequence)
    """

    __tablename__ = "audit_event"

    # id — BIGINT GENERATED ALWAYS AS IDENTITY (composite PK in __table_args__)
    id: Mapped[int] = mapped_column(
        BigInteger,
        autoincrement=True,
    )

    # UUID — UNIQUE (not a PK column)
    event_id: Mapped[uuid.UUID] = mapped_column(
        unique=True,
    )

    # Partition key + PK component (composite PK in __table_args__)
    occurred_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Correlation + sequence for ordering
    correlation_id: Mapped[str] = mapped_column(
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )

    # Event taxonomy
    event_type: Mapped[str] = mapped_column(
        nullable=False,
    )

    # Agent identity
    role: Mapped[str | None] = mapped_column(nullable=True)
    deployment: Mapped[str | None] = mapped_column(nullable=True)
    actor: Mapped[str | None] = mapped_column(nullable=True)
    tool_name: Mapped[str | None] = mapped_column(nullable=True)

    # Policy decision
    policy_decision: Mapped[str | None] = mapped_column(nullable=True)
    policy_reason: Mapped[str | None] = mapped_column(nullable=True)

    # Payload — JSONB (Postgres JSONB binary storage)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    # PII keys — TEXT[] (Postgres array of text)
    pii_keys: Mapped[list[str] | None] = mapped_column(
        ARRAY(String),
        nullable=True,
    )

    __table_args__ = (
        # Composite PRIMARY KEY — (occurred_at, id)
        PrimaryKeyConstraint("occurred_at", "id", name="pk_audit_event"),
        # Composite UNIQUE — (occurred_at, correlation_id, sequence)
        UniqueConstraint(
            "occurred_at",
            "correlation_id",
            "sequence",
            name="uq_audit_event_correlation_sequence",
        ),
        # BTree index: role + occurred_at DESC (most selective first)
        Index("ix_audit_event_role_occurred", "role", occurred_at.desc()),
        # BTree index: tool_name + occurred_at DESC — partial
        Index("ix_audit_event_tool_occurred", "tool_name", occurred_at.desc()),
        # GIN index on payload for containment queries
        Index(
            "ix_audit_event_payload_gin",
            "payload",
            postgresql_using="gin",
        ),
    )


def map_to_audit_event(event_data: dict[str, Any]) -> AuditEvent:
    """Convert a Pydantic AuditEvent sub-model instance (as dict) to an AuditEvent ORM row.

    This mapper lives here (PR-1) so that PR-2's Pydantic models can depend on it.
    The Pydantic discriminated union events are NOT yet defined — this accepts
    a dict that matches the shape of the eventual AuditEvent Pydantic sub-model.

    Args:
        event_data: Dict with at minimum event_id, occurred_at, correlation_id,
            sequence, event_type, payload. Optional: role, deployment, actor,
            tool_name, policy_decision, policy_reason, pii_keys.

    Returns:
        An unsaved AuditEvent ORM instance.
    """
    return AuditEvent(
        event_id=event_data["event_id"],
        occurred_at=event_data.get("occurred_at", datetime.now(timezone.utc)),
        correlation_id=event_data["correlation_id"],
        sequence=event_data["sequence"],
        event_type=event_data["event_type"],
        role=event_data.get("role"),
        deployment=event_data.get("deployment"),
        actor=event_data.get("actor"),
        tool_name=event_data.get("tool_name"),
        policy_decision=event_data.get("policy_decision"),
        policy_reason=event_data.get("policy_reason"),
        payload=event_data["payload"],
        pii_keys=event_data.get("pii_keys"),
    )
