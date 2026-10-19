"""Add the end-user subject key to audit events.

Revision ID: 008
Revises: 007
Create Date: 2026-10-07

ADR-011 part 2: every audit event of a turn names the end user the turn was
for. The column holds the subject KEY (``participant:<id>``), never an
address. Nullable and additive: a turn without a subject, and every row
written before this migration, leaves it NULL. ``ADD COLUMN`` without a
default is a catalog-only change on a partitioned table; PostgreSQL applies
it to every partition.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_event", sa.Column("subject", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_event", "subject")
