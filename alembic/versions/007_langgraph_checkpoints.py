"""Create the durable conversation-thread tables (LangGraph checkpoints).

Revision ID: 007
Revises: 006
Create Date: 2026-09-29

Issue #24 moves the checkpointer that holds every conversation thread from a
non-persistent Redis to this PostgreSQL database, the store that already
holds the durable webhook inbox and outbox (002-004).

The tables belong to ``langgraph-checkpoint-postgres``: it reads and writes
them and versions their schema in its own ``checkpoint_migrations`` table.
This revision does not copy that DDL. It runs the saver's own migrations
(``services.checkpointer.apply_checkpoint_migrations``), so the tables are
created in the deployment's migration step, by the migration role, alongside
every other table -- and the app never runs DDL at boot -- while the schema
stays exactly what the installed library expects. The saver's migrations
build indexes concurrently, which cannot run inside a transaction, so they
run in an autocommit block on their own connection.

When an upgraded library ships further migrations, a new revision calls
``apply_checkpoint_migrations`` again (the app refuses to boot until then,
and ``tests/test_checkpointer_service.py`` fails first).

Downgrade drops the four tables and every conversation thread with them.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the installed saver's own checkpoint migrations."""
    from agents_system.services.checkpointer import apply_checkpoint_migrations

    database_url = op.get_bind().engine.url.render_as_string(hide_password=False)
    with op.get_context().autocommit_block():
        apply_checkpoint_migrations(database_url)


def downgrade() -> None:
    """Drop the checkpoint tables, destroying every conversation thread."""
    op.execute(
        "DROP TABLE IF EXISTS checkpoint_writes, checkpoint_blobs, checkpoints, "
        "checkpoint_migrations"
    )
