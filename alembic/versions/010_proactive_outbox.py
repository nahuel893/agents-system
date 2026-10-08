"""Add the per-user proactive outbound queue.

Revision ID: 010
Revises: 009
Create Date: 2026-10-07

ADR-011 part 5 (#251): a durable queue for messages sent with no inbound
turn. A per-user table keyed by (runtime_id, subject_key, id) and fenced by
row-level security; it holds the text and the actor but no address.
"""

from __future__ import annotations

from alembic import op

from agents_system.services.proactive_postgres import (
    TABLE,
    proactive_table_statements,
)

# revision identifiers, used by Alembic.
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in proactive_table_statements(TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP TABLE {TABLE}")
