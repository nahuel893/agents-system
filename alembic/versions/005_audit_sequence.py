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

This table is intentionally small and unpartitioned: one row per
``correlation_id``, holding only ``next_seq`` -- the LAST sequence value
handed out for it. It is NOT audit data itself (no PII, no payload, no
history) and carries none of ``audit_event``'s partitioning requirements.
``AuditSink._flush_batch`` reserves each batch's sequences here, atomically,
with one upsert per distinct ``correlation_id``, in sorted order:

    INSERT INTO audit_sequence (correlation_id, next_seq)
    VALUES (:correlation_id, :count)
    ON CONFLICT (correlation_id)
    DO UPDATE SET next_seq = audit_sequence.next_seq + EXCLUDED.next_seq
    RETURNING next_seq

executed on the SAME session/transaction the batch already commits (or rolls
back) as a whole. PostgreSQL's own row lock on each ``audit_sequence`` row
serializes concurrent writers -- any process -- so this is the actual fix for
the cross-process collision, not just a relocation of the old in-process bug.
The sorted order is what keeps two flushes from locking the same rows in
opposite orders and deadlocking. A failed batch rolls its reservation back
for free, since it shares that batch's transaction.

Backfill and its cost. ``upgrade()`` seeds one row per ``correlation_id``
already in ``audit_event`` with that correlation's ``MAX(sequence)``, so no
counter restarts at 1 under history that already used it (the shared
``"none"`` fallback is the one that matters most, but an 8-hex request id can
recur too). No index leads with ``correlation_id`` -- the unique index is
``(occurred_at, correlation_id, sequence)`` -- so this is a sequential scan
of EVERY partition, including ``DEFAULT``, plus a hash aggregate with one
group per distinct correlation_id (about one per request ever audited). It
takes only ACCESS SHARE on ``audit_event``, so concurrent inserts are not
blocked, but it runs inside the migration's transaction: on a large
``audit_event``, expect the upgrade to take as long as a full-table read, and
run it in a maintenance window. Restart every worker after upgrading: code
from before this migration still numbers from its in-process counter.

Reversible: ``downgrade()`` drops the table. Nothing else in the schema
references it (no FK — ``correlation_id`` is a free-standing string shared
with ``audit_event.correlation_id``, not a foreign key, because
``audit_event`` is partitioned and PostgreSQL does not support foreign keys
referencing a partitioned table's columns from an unpartitioned one in a way
that would help here; the two tables are linked by convention, not by
constraint). It carries no data worth preserving across a rollback: every
counter is re-derivable, and a later ``upgrade()`` re-seeds each one from the
``audit_event`` rows persisted by then.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the per-correlation sequence counter table and seed it from history."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_sequence (
            correlation_id TEXT PRIMARY KEY,
            next_seq       BIGINT NOT NULL DEFAULT 0
        )
    """)
    # Backfill (PR #72 review, finding 2). Without it the first event for an
    # existing correlation_id -- above all the shared "none" fallback -- is
    # numbered 1 again. See the module docstring for the scan cost.
    #
    # MAX(sequence), not count(*): the old per-process counters restarted at
    # 1 on every boot, so history has gaps and repeats. GREATEST instead of
    # DO NOTHING so the seed can only ever raise a counter, even against a
    # pre-existing table (CREATE TABLE IF NOT EXISTS above).
    op.execute("""
        INSERT INTO audit_sequence (correlation_id, next_seq)
        SELECT correlation_id, COALESCE(MAX(sequence), 0)
          FROM audit_event
         GROUP BY correlation_id
        ON CONFLICT (correlation_id)
        DO UPDATE SET next_seq = GREATEST(audit_sequence.next_seq, EXCLUDED.next_seq)
    """)


def downgrade() -> None:
    """Drop audit_sequence. Safe: it holds only counters, not audit history."""
    op.execute("DROP TABLE IF EXISTS audit_sequence")
