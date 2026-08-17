"""Add audit_event table with monthly range partitioning.

Revision ID: 001
Revises:
Create Date: 2026-07-29

Creates the partitioned audit_event table for the D-007 audit persistence
feature. Partitioning is by RANGE (occurred_at), one partition per month.

Three monthly partitions are created at install time, plus a DEFAULT
partition. The DEFAULT one is not a nicety — it is what keeps the audit log
from failing closed on the calendar. PostgreSQL rejects an INSERT whose
partition key falls outside every partition::

    no partition of relation "audit_event" found for row

Without a DEFAULT partition, that is what the audit log does on the first day
of the fourth month after install, with no code change and no deploy. A
monthly job that adds partitions ahead of time then becomes a hard
availability dependency: miss it once and events stop being recorded. With a
DEFAULT partition present, rows always land, and adding monthly partitions
stays what it should be — an optimization for query pruning and retention.

Cost of the DEFAULT partition, stated plainly: while it holds rows, attaching
a new partition whose range would cover any of them takes an ACCESS EXCLUSIVE
lock and scans it. Keep it small by adding monthly partitions on schedule.

Partition naming is uniform ``audit_event_YYYY_MM``, computed here so the name
and the range it holds always agree. Relative names (``_current``, ``_next``)
were wrong within a month of install and could not be enumerated by pattern.

PostgreSQL requirement: all PRIMARY KEY and UNIQUE constraints on a
partitioned table must include all partition key columns.
Therefore:
- PRIMARY KEY is (occurred_at, id)
- UNIQUE is (occurred_at, correlation_id, sequence)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from alembic import op

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None

#: Monthly partitions created at install time, counting from the current month.
#: Three gives roughly a quarter of pruning-friendly storage before the DEFAULT
#: partition starts collecting; the monthly job extends the run.
_INITIAL_MONTHS = 3


def _month_start(year: int, month: int) -> date:
    """First day of ``month``, normalizing a 13th month to the next January."""
    return date(year + (month - 1) // 12, (month - 1) % 12 + 1, 1)


def partition_name(start: date) -> str:
    """``audit_event_YYYY_MM`` for the month beginning at *start*."""
    return f"audit_event_{start.year}_{start.month:02d}"


def create_monthly_partition(start: date) -> None:
    """Attach one monthly partition covering ``[start, start + 1 month)``.

    Bounds are half-open, which is PostgreSQL's own convention for RANGE
    partitions, so consecutive months tile with no gap and no overlap.

    Written as UTC timestamps rather than bare dates: ``occurred_at`` is
    TIMESTAMPTZ, and a bare date literal would be read in the session's
    TimeZone, putting the boundary hours off for any non-UTC session and
    splitting a month differently depending on who ran the migration.
    """
    end = _month_start(start.year, start.month + 1)
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {partition_name(start)} "
        f"PARTITION OF audit_event FOR VALUES "
        f"FROM ('{start.isoformat()} 00:00:00+00') "
        f"TO ('{end.isoformat()} 00:00:00+00')"
    )


def upgrade() -> None:
    """Create the audit_event parent, its initial partitions, and its indexes."""
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

    # Install-time monthly partitions, named for the month each one holds.
    # UTC, matching the TIMESTAMPTZ bounds above: at the edges of the day the
    # local date can be the previous or next month, which would name the first
    # partition for a month whose range it does not cover.
    today = datetime.now(timezone.utc).date()
    first = _month_start(today.year, today.month)
    for offset in range(_INITIAL_MONTHS):
        create_monthly_partition(_month_start(first.year, first.month + offset))

    # Catch-all. See the module docstring: without this the table stops
    # accepting rows once the monthly partitions above run out.
    op.execute(
        "CREATE TABLE IF NOT EXISTS audit_event_default "
        "PARTITION OF audit_event DEFAULT"
    )

    # Indexes on the parent, inherited by every partition including new ones.
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
    """Drop audit_event and every partition attached to it.

    Dropping the parent of a partitioned table drops its partitions, so this
    also removes partitions the monthly job added after install. Enumerating
    partition names here instead would orphan those and then fail on the
    parent — the reason this is one statement.
    """
    op.execute("DROP TABLE IF EXISTS audit_event")
