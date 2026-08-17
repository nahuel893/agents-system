"""Which tables the ORM may create, and which Alembic alone owns.

``audit_event`` is RANGE-partitioned by ``occurred_at``. SQLAlchemy has no way
to express partitioning, so ``Base.metadata.create_all`` does not fail on
PostgreSQL — it quietly emits a plain, UNPARTITIONED ``audit_event``, a
different table than the one Alembic's migration builds. On SQLite it fails
outright, because the composite PK ``(occurred_at, id)`` has an autoincrement
column.

Both outcomes come from the same mistake: creating this table from ORM
metadata. So every call site that does that — ``scripts/init_db.py`` in
production exactly as much as the test fixtures — must skip it.

These tests pin the rule where it is enforced, and pin it as a property the
table *declares* rather than a name someone has to remember to add to a list.
A second partitioned table should inherit the behavior for free.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from agentsys.models import Base
from agentsys.models.audit_event import AuditEvent
from agentsys.models.base import alembic_owned_tables, create_orm_owned_tables


def test_audit_event_declares_itself_alembic_owned() -> None:
    """The table carries the flag itself — no external name list."""
    assert AuditEvent.__table__.info.get("alembic_owned") is True


def test_alembic_owned_tables_finds_it_by_the_flag() -> None:
    """Discovery is by declaration, so a new partitioned table is picked up."""
    assert "audit_event" in {table.name for table in alembic_owned_tables()}


def test_every_other_table_is_orm_owned() -> None:
    """Only the partitioned table opts out. A typo'd flag would show up here."""
    owned = {table.name for table in alembic_owned_tables()}
    assert owned == {"audit_event"}


async def test_create_orm_owned_tables_succeeds_on_sqlite() -> None:
    """The regression this helper exists for.

    ``Base.metadata.create_all`` against SQLite raises
    ``CompileError: SQLite does not support autoincrement for composite
    primary keys`` as soon as any module has imported the audit model — which
    made the failure depend on test import order.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(create_orm_owned_tables)
            names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()

    assert "audit_event" not in names
    assert names == {table.name for table in Base.metadata.sorted_tables} - {
        "audit_event"
    }


async def test_plain_create_all_still_fails_on_sqlite() -> None:
    """Pins WHY the helper is needed.

    If a future SQLAlchemy or model change makes this pass, the helper's
    SQLite justification is gone and this test says so out loud instead of
    letting the exclusion linger unexplained. The PostgreSQL justification
    (unpartitioned DDL) survives regardless, so the helper stays either way.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    try:
        with pytest.raises(Exception, match="composite primary key"):
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
