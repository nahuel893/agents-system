"""Let a retention purge reach the expired per-user facts.

Revision ID: 011
Revises: 010
Create Date: 2026-10-08

Issue #250 review fix: the facts' row-level security leaves a purge across
subjects no scope to run in, so the facts of a user who never returns stayed
stored past their expiry. This adds a narrow maintenance path: only inside a
transaction that sets ``app.maintenance = 'purge_expired'`` are rows past
``expires_at`` visible and deletable. Live rows stay fenced to their subject,
nothing becomes writable, and a transaction without the setting is unchanged.
"""

from __future__ import annotations

from alembic import op

from agents_system.services.scope_postgres import (
    expired_purge_policy_statements,
    subject_policy_statements,
)

# revision identifiers, used by Alembic.
revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in expired_purge_policy_statements("subject_facts"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS subject_scope_purge_select ON subject_facts")
    op.execute("DROP POLICY IF EXISTS subject_scope_purge_delete ON subject_facts")
    for statement in subject_policy_statements("subject_facts"):
        op.execute(statement)
