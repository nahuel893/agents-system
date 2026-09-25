"""Add audit_sequence: a per-correlation atomic counter for audit_event.sequence.

Revision ID: 005
Revises: 004
Create Date: 2026-09-25

Fixes issue #9 (ADR-001 D-042): ``audit/recorder.py`` used to allocate
``sequence`` from a module-level ``dict[str, int]`` guarded by an
``asyncio.Lock``. That is correct within one process and wrong across N —
every worker process counts the ``"none"`` fallback ``correlation_id`` (used
whenever there is no bound request context) from 1 independently, so two
workers emitting a contextless event in the same instant can allocate the
same sequence and collide on
``uq_audit_event_correlation_sequence (occurred_at, correlation_id, sequence)``.
An in-process counter cannot be patched into correctness for this; the
counter itself has to move somewhere every process shares — the database.

This table is intentionally tiny and unpartitioned: one row per
``correlation_id``, holding only the next sequence value to hand out. It is
NOT audit data itself (no PII, no payload, no history) and carries none of
``audit_event``'s partitioning requirements. ``AuditSink._flush_batch``
allocates the real, authoritative sequence for each event here, atomically,
via the classic PostgreSQL upsert:

    INSERT INTO audit_sequence (correlation_id, next_seq)
    VALUES (:correlation_id, 1)
    ON CONFLICT (correlation_id)
    DO UPDATE SET next_seq = audit_sequence.next_seq + 1
    RETURNING next_seq

executed on the SAME session/transaction the batch already commits (or rolls
back) as a whole. PostgreSQL's own row lock on that one ``audit_sequence`` row
serializes concurrent writers -- any process -- so this is the actual fix for
the cross-process collision, not just a relocation of the old in-process bug.
A failed batch rolls its allocation back for free, since it shares that
batch's transaction: no separate reservation/release bookkeeping needed.

Reversible: ``downgrade()`` drops the table. Nothing else in the schema
references it (no FK — ``correlation_id`` is a free-standing string shared
with ``audit_event.correlation_id``, not a foreign key, because
``audit_event`` is partitioned and PostgreSQL does not support foreign keys
referencing a partitioned table's columns from an unpartitioned one in a way
that would help here; the two tables are linked by convention, not by
constraint). Downgrading loses only the in-flight counters -- restarting
after a downgrade+upgrade cycle would resume every correlation_id's sequence
at 1, so this should not be downgraded while entries for the current calendar
day still matter to sequence ordering. It carries no data worth preserving
across a rollback: unlike audit_event's own rows, an empty audit_sequence row
recreates itself correctly on the next event for that correlation_id.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the tiny per-correlation sequence counter table."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_sequence (
            correlation_id TEXT PRIMARY KEY,
            next_seq       BIGINT NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    """Drop audit_sequence. Safe: it holds only counters, not audit history."""
    op.execute("DROP TABLE IF EXISTS audit_sequence")
