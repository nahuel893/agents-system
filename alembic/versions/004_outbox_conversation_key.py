"""Add a per-conversation ordering key to outbox work.

Revision ID: 004
Revises: 003
Create Date: 2026-09-23

Fixes a blocker found in independent review of #46: nothing prevented two
messages from the SAME sender being claimed and processed concurrently, which
raced on the same checkpointer thread_id and could deliver replies out of
arrival order. The claim query (``services/outbox.pending_outbox_statement``)
now claims only the globally OLDEST non-terminal row of a given
``conversation_key`` and skips a conversation entirely while an older,
non-terminal sibling still exists for it -- whether that sibling is
live-leased, waiting out retry backoff, or simply unclaimed. Order wins over
latency: a later message of a conversation waits behind an earlier one still
in backoff, and only that earlier item's own terminal `failed` state (not a
lease expiry) releases the conversation's queue.

This migration adds the column (backfilled from the already-committed inbox
payload -- see ``_extract_conversation_key`` below, which mirrors
``webhook_worker._extract_inbound_turn``'s own envelope walk), a supporting
index for that per-conversation lookup, and a partial unique index that is
a defense-in-depth backstop: at most one non-terminal row per
``conversation_key`` may hold a lease at a time, so two workers cannot both
end up holding one conversation even under a pathological concurrent-claim
race. The claim query's own correlated NOT EXISTS is the primary mechanism;
this index never fires in the intended code path, only if that invariant is
ever broken by a future change.

Like 002/003, downgrade refuses while incomplete outbox work exists -- this
column is now load-bearing for the claim query, so dropping it under live
pending/leased rows would silently break ordering for that in-flight work
rather than just losing history.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

_INCOMPLETE_WORK_EXISTS = """
    SELECT EXISTS (
        SELECT 1 FROM outbox_work WHERE completed_at IS NULL AND failed_at IS NULL
    )
"""


def _extract_conversation_key(payload: dict[str, Any], meta_message_id: str) -> str | None:
    """Find the sender ("from") of the message matching *meta_message_id*.

    Mirrors ``webhook_worker._extract_inbound_turn``'s envelope walk, but
    only needs the sender, not the message text.
    """
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                if message.get("id") != meta_message_id:
                    continue
                sender = message.get("from")
                return sender if isinstance(sender, str) and sender else None
    return None


def upgrade() -> None:
    """Add, backfill, then lock down the conversation key column."""
    op.add_column(
        "outbox_work", sa.Column("conversation_key", sa.Text(), nullable=True)
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT ow.id, wi.meta_message_id, wi.payload "
            "FROM outbox_work ow "
            "JOIN webhook_inbox wi ON wi.id = ow.inbound_message_id"
        )
    ).fetchall()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        sender = _extract_conversation_key(payload, row.meta_message_id)
        # A message the walk cannot resolve (malformed/non-text/missing
        # sender) gets its own singleton key rather than a shared/NULL one --
        # it never collides with, or blocks, any other conversation.
        conversation_key = sender or f"unresolved:{row.meta_message_id}"
        bind.execute(
            sa.text(
                "UPDATE outbox_work SET conversation_key = :key WHERE id = :id"
            ),
            {"key": conversation_key, "id": row.id},
        )

    op.alter_column("outbox_work", "conversation_key", nullable=False)

    # Supports the claim query's correlated "does an older non-terminal
    # sibling of this conversation exist" lookup.
    op.create_index(
        "ix_outbox_work_conversation_active",
        "outbox_work",
        ["conversation_key", "enqueued_at", "id"],
        postgresql_where=sa.text("completed_at IS NULL AND failed_at IS NULL"),
    )
    # Defense-in-depth backstop (see module docstring): at most one
    # non-terminal, leased row per conversation at a time.
    op.create_index(
        "ux_outbox_work_conversation_live_lease",
        "outbox_work",
        ["conversation_key"],
        unique=True,
        postgresql_where=sa.text(
            "completed_at IS NULL AND failed_at IS NULL "
            "AND lease_expires_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    """Refuse while incomplete work exists; the column is now load-bearing."""
    op.execute("LOCK TABLE outbox_work IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(_INCOMPLETE_WORK_EXISTS)):
        raise RuntimeError(
            "Refusing to downgrade 004 while pending or leased outbox work "
            "exists -- conversation_key is load-bearing for the claim query"
        )

    op.drop_index("ux_outbox_work_conversation_live_lease", table_name="outbox_work")
    op.drop_index("ix_outbox_work_conversation_active", table_name="outbox_work")
    op.drop_column("outbox_work", "conversation_key")
