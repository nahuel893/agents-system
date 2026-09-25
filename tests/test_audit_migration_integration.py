"""The audit_event migration, applied for real against PostgreSQL.

Run with::

    AUDIT_TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/db \\
      uv run pytest -m integration tests/test_audit_migration_integration.py -v

These cannot be unit tests. ``audit_event`` is RANGE partitioned, and
partitioning is the one part of this schema SQLAlchemy cannot express — the ORM
model has no partitions at all, so a model-level test proves nothing about
them. SQLite cannot compile the DDL either. Only a real PostgreSQL round trip
shows whether a row actually lands somewhere.

They target a DEDICATED database and run ``alembic downgrade base`` in
teardown, because they apply real migrations. Do not point
``AUDIT_TEST_DATABASE_URL`` at a database whose contents you care about.

WARNING for whoever adds tests here: this file is only coverage if CI runs it.
``addopts = -m 'not integration'`` deselects it by default, and this project
has been bitten by exactly that — the sole check that the BI role was read-only
sat behind the marker and never ran anywhere. It is wired into the
``audit-migration`` CI job; keep it there.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agents_system.models.base import get_engine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _require_test_database_url() -> str:
    url = os.environ.get("AUDIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "AUDIT_TEST_DATABASE_URL not set — export a connection string for a "
            "THROWAWAY database; these tests run alembic upgrade/downgrade."
        )
    return url


def _run_alembic(command: str, url: str) -> None:
    """Invoke the alembic CLI the way an operator would.

    A subprocess, not alembic's Python API, so what runs is exactly the
    documented deployment step — including alembic.ini and env.py, which is
    where a real install can break.
    """
    result = subprocess.run(
        ["uv", "run", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {command} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


@pytest.fixture
async def migrated_engine() -> AsyncIterator[AsyncEngine]:
    url = _require_test_database_url()
    _run_alembic("upgrade head", url)
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()
        _run_alembic("downgrade base", url)


async def _partition_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT child.relname
                  FROM pg_inherits
                  JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
                  JOIN pg_class child  ON child.oid  = pg_inherits.inhrelid
                 WHERE parent.relname = 'audit_event'
                """
            )
        )
        return {row[0] for row in result}


async def test_migration_creates_a_partitioned_table(
    migrated_engine: AsyncEngine,
) -> None:
    """The parent is partitioned — 'p' in pg_class.relkind, not an ordinary 'r'.

    Cast to text in SQL: ``relkind`` is PostgreSQL's ``"char"`` type, which
    asyncpg hands back as ``b'p'``.
    """
    async with migrated_engine.connect() as conn:
        relkind = await conn.scalar(
            text("SELECT relkind::text FROM pg_class WHERE relname = 'audit_event'")
        )
    assert relkind == "p", (
        f"audit_event.relkind is {relkind!r}, expected 'p' (partitioned table). "
        "'r' means something created it from ORM metadata instead of this migration."
    )


async def test_a_row_far_outside_every_monthly_partition_is_still_accepted(
    migrated_engine: AsyncEngine,
) -> None:
    """The regression that matters.

    The migration creates a bounded number of monthly partitions. Without a
    DEFAULT partition, the first insert past the last bound fails with
    ``no partition of relation "audit_event" found for row`` — so the audit log
    stops accepting events some fixed number of months after install, with no
    code change and no warning. A DEFAULT partition makes that impossible.
    """
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO audit_event
                    (event_id, occurred_at, correlation_id, sequence,
                     event_type, payload)
                VALUES
                    (gen_random_uuid(), now() + INTERVAL '10 years',
                     'far-future-probe', 1, 'tool_call', '{}'::jsonb)
                """
            )
        )
        landed = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_event "
                "WHERE correlation_id = 'far-future-probe'"
            )
        )
    assert landed == 1


async def test_default_partition_exists_and_is_the_catch_all(
    migrated_engine: AsyncEngine,
) -> None:
    """Names the mechanism, so a regression says WHICH guarantee was removed."""
    names = await _partition_names(migrated_engine)
    assert "audit_event_default" in names, (
        f"no DEFAULT partition among {sorted(names)} — unbounded occurred_at "
        "values will be rejected once the monthly partitions run out."
    )

    async with migrated_engine.connect() as conn:
        expr = await conn.scalar(
            text(
                "SELECT pg_get_expr(relpartbound, oid) FROM pg_class "
                "WHERE relname = 'audit_event_default'"
            )
        )
    assert expr == "DEFAULT"


async def test_monthly_partitions_are_named_for_the_month_they_hold(
    migrated_engine: AsyncEngine,
) -> None:
    """Uniform ``audit_event_YYYY_MM`` naming.

    The install-time partitions and the ones added later must follow one
    convention. Relative names like ``_current``/``_next`` become wrong the
    moment the month rolls over, and they cannot be matched by pattern when
    something needs to enumerate or drop partitions.
    """
    monthly = {
        n for n in await _partition_names(migrated_engine) if n != "audit_event_default"
    }
    assert monthly, "expected at least one monthly partition"
    for name in monthly:
        suffix = name.removeprefix("audit_event_")
        year, _, month = suffix.partition("_")
        assert year.isdigit() and len(year) == 4, f"{name}: bad year in name"
        assert month.isdigit() and len(month) == 2, f"{name}: bad month in name"
        assert 1 <= int(month) <= 12, f"{name}: month out of range"


async def test_downgrade_removes_partitions_created_after_install(
    migrated_engine: AsyncEngine,
) -> None:
    """``downgrade`` must not depend on a hardcoded list of partition names.

    Dropping the parent of a partitioned table drops its partitions, so a
    partition added by the monthly job is removed too. An implementation that
    enumerates names instead would leave it orphaned and fail the drop.
    """
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE audit_event_2099_01 PARTITION OF audit_event "
                "FOR VALUES FROM ('2099-01-01') TO ('2099-02-01')"
            )
        )

    url = _require_test_database_url()
    _run_alembic("downgrade base", url)

    async with migrated_engine.connect() as conn:
        remaining = await conn.scalar(
            text("SELECT count(*) FROM pg_class WHERE relname LIKE 'audit_event%'")
        )
    assert remaining == 0, (
        "downgrade left audit_event relations behind — most likely it drops a "
        "fixed list of partition names rather than the parent table."
    )

    # The fixture's teardown downgrade must stay a no-op, not an error.
    _run_alembic("upgrade head", url)


class TestOrmMatchesTheMigration:
    """D-043: catch ORM/DDL divergence as a class, not one column at a time.

    D-043 shipped because `occurred_at` was annotated `Mapped[datetime]` with
    no explicit type: SQLAlchemy inferred `DateTime(timezone=False)`, the
    migration created TIMESTAMPTZ, and every INSERT was rejected by asyncpg
    while all 634 tests passed.

    Pinning that one column would leave the class of defect alive -- and it
    already was: review of the D-043 fix found `payload` declared generic JSON
    against a JSONB column (surviving only on an assignment cast, and making
    the GIN index below it un-creatable), plus an `event_id` `unique=True`
    that PostgreSQL cannot honour on a partitioned table and the migration
    never created.

    This compares every column the ORM compiles against the DDL Alembic
    actually produced, so the next drift fails here instead of in production.
    """

    # PostgreSQL treats unbounded VARCHAR and TEXT as the same type -- same
    # storage, same performance, no truncation on either. SQLAlchemy renders
    # `Mapped[str]` as bare VARCHAR while the migration writes TEXT, so nine
    # columns differ textually and none differ in behaviour.
    #
    # Only the UNBOUNDED form is normalised: `VARCHAR(50)` compiles to
    # "VARCHAR(50)", never matches these keys, and stays a real difference --
    # which it is, since it would truncate where TEXT does not.
    _EQUIVALENT_TYPES: ClassVar[dict[str, str]] = {
        "VARCHAR": "TEXT",
        "VARCHAR[]": "TEXT[]",
    }

    @classmethod
    def _normalise(cls, compiled_type: str) -> str:
        return cls._EQUIVALENT_TYPES.get(compiled_type, compiled_type)

    async def test_every_column_type_matches_the_migrated_ddl(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Each ORM column must compile to the type the migration created."""
        from sqlalchemy import MetaData, Table
        from sqlalchemy.dialects import postgresql

        from agents_system.models.audit_event import AuditEvent

        async with migrated_engine.connect() as conn:
            reflected = await conn.run_sync(
                lambda sync_conn: Table(
                    "audit_event", MetaData(), autoload_with=sync_conn
                )
            )

        dialect = postgresql.dialect()
        mismatches = []
        for column in AuditEvent.__table__.c:
            orm_type = self._normalise(column.type.compile(dialect))
            ddl_type = self._normalise(reflected.c[column.name].type.compile(dialect))
            if orm_type != ddl_type:
                mismatches.append(f"{column.name}: ORM {orm_type} != DDL {ddl_type}")

        assert not mismatches, (
            "the ORM and the migration disagree on column types, which fails "
            "silently at INSERT time rather than at startup:\n  "
            + "\n  ".join(mismatches)
        )

    async def test_orm_declares_no_constraint_the_database_lacks(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """A `unique=True` the migration never created is a constraint that
        exists only in the ORM's mental model.

        PostgreSQL requires every UNIQUE on a partitioned table to include the
        partition key, so `unique=True` on a lone column is DDL it refuses.
        Declaring it anyway makes idempotency logic look protected when two
        identical rows insert cleanly.
        """
        from sqlalchemy import inspect

        from agents_system.models.audit_event import AuditEvent

        def _unique_columns(sync_conn: object) -> set[str]:
            inspector = inspect(sync_conn)
            found = {
                frozenset(uc["column_names"])
                for uc in inspector.get_unique_constraints("audit_event")
            }
            indexes = {
                frozenset(ix["column_names"])
                for ix in inspector.get_indexes("audit_event")
                if ix.get("unique")
            }
            return {next(iter(c)) for c in (found | indexes) if len(c) == 1}

        async with migrated_engine.connect() as conn:
            single_column_uniques = await conn.run_sync(_unique_columns)

        claimed = {c.name for c in AuditEvent.__table__.c if c.unique}
        phantom = claimed - single_column_uniques
        assert not phantom, (
            f"ORM claims UNIQUE on {sorted(phantom)}, but the migrated table "
            "has no such constraint -- duplicates insert cleanly"
        )


class TestAuditSinkWritesThroughTheProductionPath:
    """The round trip the unit suite cannot do: mapper -> ORM -> PostgreSQL.

    Every other audit test either mocks the session or asserts on DDL. The
    production write is `event.model_dump()` -> `map_to_audit_event(...)` ->
    `session.add` (sink.py), and none of it was ever exercised against a real
    database -- which is why a column-type mismatch rejected 100% of audit
    writes with the suite green.

    So this drives `map_to_audit_event` itself rather than hand-building a row:
    a regression in the mapper reproduces D-043 exactly, and the drainer's
    blanket `except Exception` would swallow it just the same.
    """

    async def test_mapper_output_round_trips_into_the_right_partition(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """A mapped row must persist, keep its offset, and land in its month."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from agents_system.models.audit_event import map_to_audit_event

        occurred = datetime.now(UTC)
        event_id = uuid.uuid4()

        # Shaped exactly like the dict sink.py hands the mapper.
        row = map_to_audit_event(
            {
                "event_id": event_id,
                "occurred_at": occurred,
                "correlation_id": "d043-roundtrip",
                "sequence": 1,
                "event_type": "tool_granted",
                "role": "sales-agent",
                "deployment": "acme",
                "payload": {"tool_name": "catalog_search"},
                "pii_keys": [],
            }
        )

        session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add(row)
            await session.commit()

        async with migrated_engine.connect() as conn:
            stored, partition = (
                await conn.execute(
                    text(
                        "SELECT occurred_at, tableoid::regclass::text "
                        "FROM audit_event WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
            ).one()

        assert stored.tzinfo is not None, (
            "occurred_at came back naive -- the column lost its time zone"
        )
        assert stored == occurred

        # The EXACT monthly partition, not merely "not the parent". Asserting
        # `!= "audit_event"` is satisfied by audit_event_default, so it stays
        # green precisely when the monthly partitions are missing or misdated
        # -- the one failure the audit-migration CI job exists to catch.
        assert partition == f"audit_event_{occurred:%Y_%m}", (
            f"row landed in {partition!r}, not its month's partition; "
            "audit_event_default means the monthly bounds are wrong"
        )


class TestAuditSequenceAllocationIsProcessSafe:
    """Issue #9 (ADR-001 D-042) — the one claim a mock cannot prove.

    The former `_allocate_sequence`/`_seq_counter` was a module-level dict:
    correct within one process, wrong across N, because every process
    counted the `"none"` fallback correlation_id from 1 independently. A
    mock-based unit test cannot show that failure mode -- mocks share one
    process's memory, so "two workers" collapse into one. Only a real
    PostgreSQL round trip, through two genuinely separate connections/engines
    driving concurrent `AuditSink._flush_batch` calls for the SAME
    correlation_id, proves the atomic upsert (`audit_sequence`, migration
    005) actually serializes them.
    """

    async def test_two_concurrent_flushes_allocate_a_gapless_duplicate_free_sequence(
        self, migrated_engine: AsyncEngine
    ) -> None:
        """Two 'workers' flushing concurrently for one correlation_id must not
        collide, lose rows, or leave gaps in the allocated sequence.
        """
        from unittest.mock import patch

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from agents_system.audit.events import ToolCallAttempted
        from agents_system.audit.sink import AuditSink

        url = _require_test_database_url()
        correlation_id = f"process-safety-{uuid.uuid4()}"
        events_per_worker = 5
        total = events_per_worker * 2

        def _make_batch() -> list[ToolCallAttempted]:
            return [
                ToolCallAttempted(
                    event_id=uuid.uuid4(),
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id,
                    sequence=0,  # placeholder; _flush_batch must overwrite it
                    role="test-role",
                    tool_name="test_tool",
                    payload={"tool_name": "test_tool"},
                    pii_keys=[],
                )
                for _ in range(events_per_worker)
            ]

        # Two independent engines -- two independent connection pools, the
        # closest a single test process can get to "two worker processes"
        # without actually forking. One engine/one connection would let
        # PostgreSQL trivially serialize the two flushes for free and prove
        # nothing about the allocator itself.
        worker_engine_a = get_engine(url)
        worker_engine_b = get_engine(url)
        try:
            sink_a = AuditSink(
                session_factory=async_sessionmaker(
                    worker_engine_a, expire_on_commit=False
                )
            )
            sink_b = AuditSink(
                session_factory=async_sessionmaker(
                    worker_engine_b, expire_on_commit=False
                )
            )

            with patch("agents_system.audit.sink.logger") as mock_logger:
                await asyncio.gather(
                    sink_a._flush_batch(_make_batch()),
                    sink_b._flush_batch(_make_batch()),
                )
                # _flush_batch swallows and logs any commit failure (it must
                # never crash the drainer) -- a unique-constraint collision on
                # `uq_audit_event_correlation_sequence` would surface here as
                # a call to `logger.exception`, not as a raised exception.
                mock_logger.exception.assert_not_called()
        finally:
            await worker_engine_a.dispose()
            await worker_engine_b.dispose()

        async with migrated_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT sequence FROM audit_event WHERE correlation_id = :cid"),
                {"cid": correlation_id},
            )
            sequences = sorted(row[0] for row in result)

        assert len(sequences) == total, (
            f"expected {total} rows for {correlation_id!r}, got {len(sequences)}: "
            f"{sequences} -- a missing row means a batch silently lost a "
            "unique-constraint collision"
        )
        assert len(set(sequences)) == total, f"duplicate sequence values: {sequences}"
        assert sequences == list(range(1, total + 1)), (
            f"sequence must be contiguous and gap-free: {sequences}"
        )
