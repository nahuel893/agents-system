"""The `agentsys_* -> agents_system_*` view rename migration, run for real.

`tests/test_view_rename_migration.py` checks the migration script statically
-- no database, so it cannot prove the SQL actually runs or that it leaves a
working view behind. This does: it renames the demo company's own views back
to their pre-#45 `agentsys_*` names (simulating a deployment that created them
before the package rename), runs
`demo/migrations/0001_rename_agentsys_to_agents_system_views.sql` against
that, and asserts both that the contract views exist again under the new
names and that a report still returns the seeded figures through them. It
then runs the script a second time to prove the `IF EXISTS` guards make that
a no-op rather than an error.

Marked `integration` because it needs a live Postgres; it runs in the
`demo-reports` CI job, which already loads the demo company for
`test_sales_reports_integration.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.connectors.sales_reports import CATALOG, CONTRACT_VIEWS
from agents_system.services.reports import run_report

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_SQL = (
    _REPO_ROOT
    / "demo"
    / "migrations"
    / "0001_rename_agentsys_to_agents_system_views.sql"
).read_text()
_DEFAULT_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo"
)

#: `sale_count` for `confirmed`, straight from `demo/company/03_seed.sql`,
#: the same way `test_sales_reports_integration.py` verifies figures --
#: independent of the module under test, so a broken migration that silently
#: dropped rows would still be caught here.
SEEDED_CONFIRMED = 288


def _demo_url() -> str:
    return os.getenv("DEMO_DATABASE_URL", _DEFAULT_URL)


@pytest.fixture(scope="module")
def demo_database() -> str:
    """Load the demo company fresh, or skip if no database is reachable.

    Module-scoped and separate from `test_sales_reports_integration.py`'s own
    fixture of the same name -- this test renames the live views mid-run, and
    keeping it in its own module means a failure here cannot leave the shared
    database in a half-migrated state for an unrelated test file.
    """
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "demo" / "load_demo_company.py")],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"demo company unavailable: {result.stderr.strip()[:300]}")
    return _demo_url()


async def _run_migration_script(conn: Any) -> None:
    # Same reason as `demo/load_demo_company.py`'s `_run_sql_files`: the
    # script is several semicolon-separated commands, and every SQLAlchemy
    # execute path sends a PREPARED statement, which Postgres refuses for
    # more than one command at a time. The raw asyncpg connection's simple
    # query protocol accepts the whole script.
    #
    # `conn` must come from `engine.connect()`, never `engine.begin()`: the
    # script itself contains `BEGIN;`/`COMMIT;`, so it is its own transaction
    # owner. Running it inside a SQLAlchemy-owned transaction would mix two
    # transaction managers on the same connection -- the script's `COMMIT`
    # would end SQLAlchemy's transaction out from under it, leaving the
    # connection's tracked state out of sync with the server.
    raw = await conn.get_raw_connection()
    driver_conn = raw.driver_connection
    assert driver_conn is not None, "no raw asyncpg connection available"
    await driver_conn.execute(_MIGRATION_SQL)


async def _view_names(conn: Any) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = 'public'"
        )
    )
    return {row[0] for row in result}


async def test_the_migration_recreates_the_contract_views_and_reports_still_work(
    demo_database: str,
) -> None:
    engine = create_async_engine(demo_database)
    try:
        #: new contract name -> the pre-#45 `agentsys_*` name it replaced.
        old_name_by_new = {
            new_name: f"agentsys_{new_name.removeprefix('agents_system_')}"
            for new_name in CONTRACT_VIEWS
        }
        old_names = set(old_name_by_new.values())

        try:
            async with engine.begin() as conn:
                # Simulate a deployment that created its views before #45:
                # put them back under the old names.
                for new_name, old_name in old_name_by_new.items():
                    await conn.execute(
                        text(f"ALTER VIEW {new_name} RENAME TO {old_name}")
                    )

                existing = await _view_names(conn)
                assert existing >= old_names
                assert not (existing & set(CONTRACT_VIEWS))

            # `engine.connect()`, not `engine.begin()`: see the note on
            # `_run_migration_script`.
            async with engine.connect() as conn:
                await _run_migration_script(conn)

            async with engine.begin() as conn:
                existing = await _view_names(conn)
                assert existing >= set(CONTRACT_VIEWS)
                assert not (existing & old_names)

            # The renamed-back views are the same objects, so a report
            # through them must recover the same seeded figures as before
            # the round trip.
            result = await run_report(
                engine, CATALOG["status_summary"], {"months_back": 24, "limit": 10}
            )
            counts = {row["status"]: row["sale_count"] for row in result["rows"]}
            assert counts["confirmed"] == SEEDED_CONFIRMED

            # Idempotency: the old names are already gone, so a second run
            # must do nothing rather than fail with "relation ... does not
            # exist".
            async with engine.connect() as conn:
                await _run_migration_script(conn)
            async with engine.begin() as conn:
                existing = await _view_names(conn)
                assert existing >= set(CONTRACT_VIEWS)
        finally:
            # No matter how far the rename-away/migrate/assert sequence
            # above got -- including a failure right after the views were
            # renamed to their pre-#45 agentsys_* names -- always re-run the
            # migration to bring them back under the agents_system_*
            # contract names. It is idempotent (`IF EXISTS` guards), so this
            # is a no-op when the sequence already succeeded and a repair
            # when it didn't. Without this, a failing test leaves the shared
            # demo database's views broken for the next run.
            async with engine.connect() as conn:
                await _run_migration_script(conn)
    finally:
        await engine.dispose()
