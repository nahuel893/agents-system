"""Per-end-user usage ledger.

Revision ID: 009
Revises: 008
Create Date: 2026-10-07

ADR-011 part 5: one row per turn, keyed by (runtime_id, subject_key, id) and
fenced by row-level security (FORCE, permissive and restrictive policy), like
every per-user table. ``cost_usd`` is NULL when the model had no price, which
is how an unpriced turn is flagged. The policy relies on the application's
database role not being a superuser or BYPASSRLS; the served app verifies it
at boot.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from agents_system.services.scope_postgres import subject_policy_statements

# revision identifiers, used by Alembic.
revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subject_usage",
        sa.Column("runtime_id", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("model_calls", sa.Integer(), nullable=True),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("runtime_id", "subject_key", "id"),
    )
    op.create_index(
        "ix_subject_usage_window",
        "subject_usage",
        ["runtime_id", "subject_key", "occurred_at"],
    )
    for statement in subject_policy_statements("subject_usage"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_subject_usage_window", table_name="subject_usage")
    op.drop_table("subject_usage")
