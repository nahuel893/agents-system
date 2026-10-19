"""Add the per-user schedules table.

Revision ID: 013
Revises: 012
Create Date: 2026-10-08

ADR-011 part 3 (#175): schedules an end user asked for. A per-user table
keyed by (runtime_id, subject_key, id) and fenced by row-level security; it
holds the request in the user's words and no address.
"""

from __future__ import annotations

from alembic import op

from agents_system.services.schedules_postgres import (
    SCHEDULES_TABLE,
    schedule_table_statements,
)

# revision identifiers, used by Alembic.
revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in schedule_table_statements(SCHEDULES_TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.execute(f"DROP TABLE {SCHEDULES_TABLE}")
