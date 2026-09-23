"""Add recoverable claim, retry, failure, and reply-replay state to outbox work.

Revision ID: 003
Revises: 002
Create Date: 2026-08-04

This migration only adds columns and partial indexes. It does not alter inbound
messages or audit-event partitioning. Downgrade holds an ACCESS EXCLUSIVE lock
through its guard and column drops, so it cannot erase state that appears after
an unlocked preflight check.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


_RECOVERABLE_STATE_EXISTS = """
    SELECT EXISTS (
        SELECT 1
        FROM outbox_work
        WHERE attempt_count <> 0
           OR lease_owner IS NOT NULL
           OR failed_at IS NOT NULL
           OR last_error IS NOT NULL
           OR outbound_body IS NOT NULL
           OR outbound_send_key IS NOT NULL
    )
"""


def upgrade() -> None:
    """Add state needed to commit recovery decisions before processing."""
    op.add_column(
        "outbox_work",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column("outbox_work", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column("outbox_work", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "outbox_work",
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_work",
        sa.Column(
            "outbound_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "outbox_work",
        sa.Column("outbound_send_key", sa.Text(), nullable=True),
    )

    # W1 has not wired acceptance yet, so this table is initially empty.
    # Keep columns and indexes in one transaction: a failed upgrade must not
    # leave columns committed with revision 002 and half-created indexes.
    op.create_index(
        "ix_outbox_work_ready_recoverable",
        "outbox_work",
        ["available_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "completed_at IS NULL AND failed_at IS NULL "
            "AND lease_expires_at IS NULL"
        ),
    )
    op.create_index(
        "ix_outbox_work_expired_recoverable",
        "outbox_work",
        ["lease_expires_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "completed_at IS NULL AND failed_at IS NULL "
            "AND lease_expires_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Guard and remove unused W2a schema in one locked transaction."""
    op.execute("LOCK TABLE outbox_work IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(_RECOVERABLE_STATE_EXISTS)):
        raise RuntimeError(
            "Refusing to downgrade 003 while recoverable outbox state exists"
        )

    op.drop_index(
        "ix_outbox_work_expired_recoverable",
        table_name="outbox_work",
    )
    op.drop_index(
        "ix_outbox_work_ready_recoverable",
        table_name="outbox_work",
    )

    op.drop_column("outbox_work", "outbound_send_key")
    op.drop_column("outbox_work", "outbound_body")
    op.drop_column("outbox_work", "failed_at")
    op.drop_column("outbox_work", "last_error")
    op.drop_column("outbox_work", "lease_owner")
    op.drop_column("outbox_work", "attempt_count")
