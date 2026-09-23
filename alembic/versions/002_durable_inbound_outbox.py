"""Create durable webhook inbox and outbox work tables.

Revision ID: 002
Revises: 001
Create Date: 2026-08-03

The tables are new and empty at upgrade time, so their indexes are created in
the migration transaction: concurrent index creation is unnecessary and cannot
run inside that transaction. No existing audit_event data is altered.

Downgrade refuses to drop incomplete outbox work. Once all rows are complete,
it drops only these new tables, destroying completed history; test downgrade
only against an isolated disposable database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one inbox table and one durable work table without touching audit data."""
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("meta_message_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_inbox"),
        sa.UniqueConstraint("meta_message_id", name="uq_webhook_inbox_meta_message_id"),
    )
    op.create_table(
        "outbox_work",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("inbound_message_id", sa.Uuid(), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["inbound_message_id"],
            ["webhook_inbox.id"],
            name="fk_outbox_work_inbound_message",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_work"),
        sa.UniqueConstraint(
            "inbound_message_id", name="uq_outbox_work_inbound_message"
        ),
    )
    op.create_index(
        "ix_outbox_work_pending",
        "outbox_work",
        ["available_at", "id"],
        postgresql_where=sa.text("completed_at IS NULL AND lease_expires_at IS NULL"),
    )
    op.create_index(
        "ix_outbox_work_expired_lease",
        "outbox_work",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text(
            "completed_at IS NULL AND lease_expires_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Drop only completed work; refuse to delete pending or leased rows."""
    has_unfinished_work = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM outbox_work WHERE completed_at IS NULL)")
    )
    if has_unfinished_work:
        raise RuntimeError(
            "Refusing to downgrade 002 while pending or leased outbox work exists"
        )
    op.drop_table("outbox_work")
    op.drop_table("webhook_inbox")
