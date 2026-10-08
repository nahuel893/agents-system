"""Per-end-user memory facts.

Revision ID: 010
Revises: 009
Create Date: 2026-10-08

ADR-011 part 3, issue #250: one row per fact, keyed by (runtime_id,
subject_key, id) and fenced by row-level security (FORCE, permissive and
restrictive policy), like every per-user table. ``id`` is a hash of the
normalized fact, so remembering the same fact twice is one row. ``expires_at``
is NULL for a fact that does not expire. The policy relies on the
application's database role not being a superuser or BYPASSRLS; the served
app verifies it at boot.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from agents_system.services.scope_postgres import subject_policy_statements

# revision identifiers, used by Alembic.
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subject_facts",
        sa.Column("runtime_id", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("runtime_id", "subject_key", "id"),
    )
    for statement in subject_policy_statements("subject_facts"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("subject_facts")
