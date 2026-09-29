"""The Postgres checkpointer service (issue #24), without a database.

The SQL itself (purge, schema check, restart survival) is proven against a
real PostgreSQL in ``test_checkpointer_integration.py``; these tests pin the
pieces that need none: the connection string, the schema-version tripwire,
and the periodic purge loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents_system.services.checkpointer import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointPurger,
    PurgeResult,
    psycopg_conninfo,
)


def _logged(out: str, key: str, value: object) -> bool:
    """Whether a log line carries ``key`` = ``value``, in either structlog
    rendering the suite runs under (console ``key=value`` or JSON)."""
    rendered = value if isinstance(value, int) else f'"{value}"'
    return f"{key}={value}" in out or f'"{key}": {rendered}' in out


# ---------------------------------------------------------------------------
# psycopg_conninfo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://app:s3cr%40t@db.internal:5433/agents",
            "postgresql://app:s3cr%40t@db.internal:5433/agents",
        ),
        (
            "postgresql://app@localhost/agents",
            "postgresql://app@localhost/agents",
        ),
        (
            "postgresql+psycopg://app@localhost/agents",
            "postgresql://app@localhost/agents",
        ),
        (
            "postgresql+asyncpg://app@localhost/agents?ssl=require",
            "postgresql://app@localhost/agents?sslmode=require",
        ),
    ],
)
def test_the_app_database_url_becomes_a_libpq_connection_string(
    url: str, expected: str
) -> None:
    """The checkpointer shares the app's database, so it derives its own
    connection string from DATABASE_URL instead of a second setting."""
    assert psycopg_conninfo(url) == expected


def test_a_non_postgres_url_is_refused() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        psycopg_conninfo("sqlite+aiosqlite:///local.db")


# ---------------------------------------------------------------------------
# Schema version tripwire
# ---------------------------------------------------------------------------


def test_the_installed_saver_needs_exactly_the_schema_the_migrations_create() -> None:
    """Alembic revision 007 applies the saver's own migrations. When a
    langgraph-checkpoint-postgres upgrade ships new ones, a database already
    at 007 will not get them from `alembic upgrade head`, and the app refuses
    to boot. This fails first: add an Alembic revision that runs
    `apply_checkpoint_migrations` again, then raise CHECKPOINT_SCHEMA_VERSION.
    """
    from langgraph.checkpoint.postgres.base import BasePostgresSaver

    assert len(BasePostgresSaver.MIGRATIONS) - 1 == CHECKPOINT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# CheckpointPurger
# ---------------------------------------------------------------------------


class _RecordingPurge:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[int | None] = []
        self.failures = failures
        self.ran = asyncio.Event()

    async def __call__(self, retention_s: int | None) -> PurgeResult:
        self.calls.append(retention_s)
        if len(self.calls) >= 3:
            self.ran.set()
        if self.failures:
            self.failures -= 1
            raise ConnectionError("database unavailable (simulated)")
        return PurgeResult(threads_deleted=2, checkpoints_pruned=5)


async def test_the_purger_enforces_the_configured_retention_on_every_run() -> None:
    purge = _RecordingPurge()
    purger = CheckpointPurger(purge, retention_s=86_400, interval_s=0.01)

    await purger.start()
    await asyncio.wait_for(purge.ran.wait(), timeout=2.0)
    await purger.stop()

    assert purge.calls[:3] == [86_400, 86_400, 86_400]


async def test_the_purger_runs_once_immediately_at_start() -> None:
    purge = _RecordingPurge()
    purger = CheckpointPurger(purge, retention_s=60, interval_s=3_600)

    await purger.start()
    for _ in range(100):
        if purge.calls:
            break
        await asyncio.sleep(0.01)
    await purger.stop()

    assert purge.calls == [60]


async def test_indefinite_retention_still_prunes_superseded_checkpoints() -> None:
    """`never` keeps idle threads, but storage hygiene still runs."""
    purge = _RecordingPurge()
    purger = CheckpointPurger(purge, retention_s=None, interval_s=3_600)

    await purger.start()
    for _ in range(100):
        if purge.calls:
            break
        await asyncio.sleep(0.01)
    await purger.stop()

    assert purge.calls == [None]


async def test_a_failed_purge_is_logged_and_the_next_one_still_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    purge = _RecordingPurge(failures=2)
    purger = CheckpointPurger(purge, retention_s=86_400, interval_s=0.01)

    await purger.start()
    await asyncio.wait_for(purge.ran.wait(), timeout=2.0)
    await purger.stop()

    out = capsys.readouterr().out
    assert "checkpointer.purge_failed" in out
    assert "checkpointer.purged" in out


async def test_stopping_the_purger_twice_or_before_start_is_harmless() -> None:
    purger = CheckpointPurger(_RecordingPurge(), retention_s=1, interval_s=1)
    await purger.stop()
    await purger.start()
    await purger.stop()
    await purger.stop()


async def test_the_purge_log_reports_counts_and_never_thread_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A thread id is a customer's phone number: the purge log carries
    counts only."""

    async def purge(retention_s: int | None) -> Any:
        return PurgeResult(threads_deleted=1, checkpoints_pruned=0)

    purger = CheckpointPurger(purge, retention_s=86_400, interval_s=3_600)
    await purger.start()
    for _ in range(100):
        if "checkpointer.purged" in (out := capsys.readouterr().out):
            break
        await asyncio.sleep(0.01)
    await purger.stop()

    assert _logged(out, "threads_deleted", 1)
    assert _logged(out, "retention_s", 86400)


def test_the_agent_state_has_no_delta_channel_so_pruning_is_safe() -> None:
    """The purge keeps only each thread's latest checkpoint. That is safe
    only while no state channel is a DeltaChannel, whose value is rebuilt from
    earlier checkpoints (see BaseCheckpointSaver.prune). Adding one must
    revisit `_PRUNE_CHECKPOINTS` first."""
    from langgraph.channels.delta import DeltaChannel
    from langgraph.graph import StateGraph

    from agents_system.agent.state import AgentState

    channels = StateGraph(AgentState).channels
    assert channels
    assert not any(isinstance(c, DeltaChannel) for c in channels.values())
