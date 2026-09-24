"""Which tables the ORM may create, and which Alembic alone owns.

``audit_event`` is RANGE-partitioned by ``occurred_at``. SQLAlchemy has no way
to express partitioning, so ``Base.metadata.create_all`` does not fail on
PostgreSQL — it quietly emits a plain, UNPARTITIONED ``audit_event``, a
different table than the one Alembic's migration builds. On SQLite it fails
outright, because the composite PK ``(occurred_at, id)`` has an autoincrement
column. ``webhook_inbox`` and ``outbox_work`` are likewise created only by
Alembic so their PostgreSQL JSONB columns, partial indexes, and constraints
match the durable delivery schema.

The same mistake is creating any of these tables from ORM metadata. So every
call site that does that must skip those tables — before #70, that meant both
``scripts/init_db.py`` and the test fixtures; ``audit_event`` was then the only
table ``Base.metadata`` declared at all, so there was nothing left for either
to bulk-create from metadata, and both stopped trying.

These tests pin the rule where it is enforced, and pin it as a property the
table *declares* rather than a name someone has to remember to add to a list.
A second partitioned table should inherit the behavior for free.
"""

from __future__ import annotations

from sqlalchemy import DateTime

from agents_system.models import Base
from agents_system.models.audit_event import AuditEvent
from agents_system.models.base import alembic_owned_tables


def test_audit_event_declares_itself_alembic_owned() -> None:
    """The table carries the flag itself — no external name list."""
    assert AuditEvent.__table__.info.get("alembic_owned") is True


def test_alembic_owned_tables_finds_it_by_the_flag() -> None:
    """Discovery is by declaration, so a new partitioned table is picked up."""
    assert "audit_event" in {table.name for table in alembic_owned_tables()}


def test_platform_tables_declare_themselves_alembic_owned() -> None:
    """Every platform-managed table opts out. A typo'd flag would show up here."""
    owned = {table.name for table in alembic_owned_tables()}
    assert owned == {"audit_event", "outbox_work", "webhook_inbox"}


def test_every_datetime_column_is_timezone_aware() -> None:
    """No column in the project may compile to TIMESTAMP WITHOUT TIME ZONE.

    Every migration here creates TIMESTAMPTZ, so a naive column is always a
    defect -- and one that cannot fail loudly: the INSERT compiles with a
    `::TIMESTAMP WITHOUT TIME ZONE` cast and asyncpg rejects the aware value at
    runtime, which for audit_event meant the drainer logged and dropped 100% of
    events (D-043) with the whole suite green.

    This lives in the default suite on purpose. The integration test comparing
    the ORM against reflected DDL is stronger, but it only runs in the
    audit-migration CI job; this one fails in ~9 seconds, on every commit, for
    every table -- including ones not written yet.
    """
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert not naive, (
        "these columns compile to TIMESTAMP WITHOUT TIME ZONE while the "
        f"migrations create TIMESTAMPTZ: {naive}. Base.type_annotation_map "
        "should make DateTime(timezone=True) the default -- check it is intact."
    )
