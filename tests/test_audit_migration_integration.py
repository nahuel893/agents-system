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

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.models.base import get_engine

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
    monthly = {n for n in await _partition_names(migrated_engine) if n != "audit_event_default"}
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
            text(
                "SELECT count(*) FROM pg_class "
                "WHERE relname LIKE 'audit_event%'"
            )
        )
    assert remaining == 0, (
        "downgrade left audit_event relations behind — most likely it drops a "
        "fixed list of partition names rather than the parent table."
    )

    # The fixture's teardown downgrade must stay a no-op, not an error.
    _run_alembic("upgrade head", url)
