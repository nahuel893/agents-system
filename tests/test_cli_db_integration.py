"""`agents db upgrade`, applied for real against PostgreSQL (issue #312).

Run with::

    CLI_TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/db \\
      uv run pytest -m integration tests/test_cli_db_integration.py -v

The command runs in a subprocess from a directory outside the repository, so
neither the checkout's ``alembic.ini`` nor its working directory can help it:
what it applies are the migrations the package carries.
``scripts/smoke-installed-wheel.sh`` runs the same command from an installed
wheel when ``SMOKE_DATABASE_URL`` is set.

It targets a DEDICATED database and downgrades it to ``base`` before and
after, because it applies real migrations. Do not point
``CLI_TEST_DATABASE_URL`` at a database whose contents you care about. Wired
into the ``cli-db-migration`` CI job; keep it there.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.integration


def _database_url() -> str:
    url = os.environ.get("CLI_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "CLI_TEST_DATABASE_URL not set — export a connection string for a "
            "THROWAWAY database; this test applies and reverts every migration."
        )
    return url


def _agents(
    *args: str, url: str, cwd: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    """`agents ...` as an operator runs it: its own process, a bare
    environment (no `.env`, no server variables), another directory."""
    return subprocess.run(
        [sys.executable, "-m", "agents_system", *args, "--database-url", url],
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _downgrade_to_base(url: str) -> None:
    from alembic import command

    from agents_system.cli.db import alembic_config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.downgrade(alembic_config(), "base")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def empty_database() -> Iterator[str]:
    url = _database_url()
    _downgrade_to_base(url)
    yield url
    _downgrade_to_base(url)


def _head() -> str:
    from alembic.script import ScriptDirectory

    from agents_system.cli.db import alembic_config

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


async def _tables(url: str) -> set[str]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def test_db_upgrade_applies_every_migration_to_an_empty_database(
    empty_database: str, tmp_path: pathlib.Path
) -> None:
    head = _head()

    upgrade = _agents("db", "upgrade", url=empty_database, cwd=tmp_path)
    assert upgrade.returncode == 0, upgrade.stderr
    assert "Running upgrade" in upgrade.stderr
    assert f"-> {head}" in upgrade.stderr

    current = _agents("db", "current", url=empty_database, cwd=tmp_path)
    assert current.returncode == 0, current.stderr
    assert f"{head} (head)" in current.stdout

    tables = await _tables(empty_database)
    # One table per stage of the history: the first revision, the outbox,
    # the checkpointer (007, required before the app boots) and the latest.
    assert {
        "alembic_version",
        "audit_event",
        "outbox_work",
        "checkpoints",
        "subject_schedules",
    } <= tables

    # Applying again is a no-op, not an error.
    again = _agents("db", "upgrade", url=empty_database, cwd=tmp_path)
    assert again.returncode == 0, again.stderr
    assert "Running upgrade" not in again.stderr
