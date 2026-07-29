"""Add audit_event table with monthly range partitioning.

Revision ID: 001
Revises:
Create Date: 2026-07-29

Creates the partitioned audit_event table for the D-007 audit persistence
feature. Partitioning is by RANGE (occurred_at) monthly.

The first three partitions (current month, current+1, current+2) are
created at install time. Additional partitions are created via the
create_audit_event_partition() helper at the bottom of this file,
intended to be called by a cron job monthly.

PostgreSQL requirement: all PRIMARY KEY and UNIQUE constraints on a
partitioned table must include all partition key columns.
Therefore:
- PRIMARY KEY is (occurred_at, id)
- UNIQUE is (occurred_at, correlation_id, sequence)
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create audit_event parent table + first 3 monthly partitions + indexes."""
    # Parent table with monthly range partitioning
    # PostgreSQL requires all PK/UK constraints to include partition key columns
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_event (
            id              BIGINT GENERATED ALWAYS AS IDENTITY,
            event_id        UUID     NOT NULL,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            correlation_id  TEXT     NOT NULL,
            sequence        BIGINT   NOT NULL,
            event_type      TEXT     NOT NULL,
            role            TEXT,
            deployment      TEXT,
            actor           TEXT,
            tool_name       TEXT,
            policy_decision TEXT     CHECK (policy_decision IN ('allowed','blocked')),
            policy_reason   TEXT,
            payload         JSONB    NOT NULL,
            pii_keys        TEXT[],
            PRIMARY KEY (occurred_at, id),
            UNIQUE (occurred_at, correlation_id, sequence)
        ) PARTITION BY RANGE (occurred_at)
    """)

    # Partition 1: current month
    op.execute("""
        CREATE TABLE audit_event_current PARTITION OF audit_event
          FOR VALUES FROM (date_trunc('month', now())::date)
                       TO (date_trunc('month', now())::date + INTERVAL '1 month')
    """)

    # Partition 2: next month
    op.execute("""
        CREATE TABLE audit_event_next PARTITION OF audit_event
          FOR VALUES FROM (date_trunc('month', now())::date + INTERVAL '1 month')
                       TO (date_trunc('month', now())::date + INTERVAL '2 months')
    """)

    # Partition 3: month after next
    op.execute("""
        CREATE TABLE audit_event_future PARTITION OF audit_event
          FOR VALUES FROM (date_trunc('month', now())::date + INTERVAL '2 months')
                       TO (date_trunc('month', now())::date + INTERVAL '3 months')
    """)

    # Indexes on parent (inherited by all partitions)
    op.execute("""
        CREATE INDEX ix_audit_event_role_occurred
          ON audit_event (role, occurred_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_audit_event_tool_occurred
          ON audit_event (tool_name, occurred_at DESC)
          WHERE tool_name IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_audit_event_payload_gin
          ON audit_event USING GIN (payload)
    """)


def downgrade() -> None:
    """Drop all partitions and the parent table.

    Partitions MUST be dropped before the parent table.
    """
    op.execute("DROP TABLE IF EXISTS audit_event_future")
    op.execute("DROP TABLE IF EXISTS audit_event_next")
    op.execute("DROP TABLE IF EXISTS audit_event_current")
    op.drop_table("audit_event")


# -----------------------------------------------------------------------------
# Helper for cron: create a future monthly partition.
# Run from an alembic revision or cron script with:
#
#   from alembic import op
#   import sys; sys.path.insert(0, '/path/to/project')
#   from alembic.versions.add_audit_event_partition import create_audit_event_partition
#   create_audit_event_partition(op, '2026-10')
# -----------------------------------------------------------------------------
def create_audit_event_partition(op_instance, month: str) -> None:
    """Create a monthly partition for audit_event.

    Args:
        op_instance: The Alembic ``op`` operations object (passed by caller).
        month: Month in 'YYYY-MM' format, e.g. '2026-10'.
    """
    import re
    from datetime import date

    if not re.match(r"^\d{4}-\d{2}$", month):
        raise ValueError(f"Invalid month format: {month!r}. Expected 'YYYY-MM'.")

    year_str, mon_str = month.split("-")
    year_i, mon_i = int(year_str), int(mon_str)

    part_name = f"audit_event_{year_i}_{mon_i:02d}"
    start = date(year_i, mon_i, 1)
    if mon_i == 12:
        end = date(year_i + 1, 1, 1)
    else:
        end = date(year_i, mon_i + 1, 1)

    op_instance.execute(f"""
        CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF audit_event
          FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')
    """)
