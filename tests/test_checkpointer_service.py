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
    CheckpointSchemaError,
    PurgeResult,
    _connection_kwargs,
    psycopg_conninfo,
    verify_checkpoint_schema,
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
        # asyncpg also spells the verify modes with an underscore.
        (
            "postgresql+asyncpg://app@localhost/agents?ssl=verify_full",
            "postgresql://app@localhost/agents?sslmode=verify-full",
        ),
        (
            "postgresql+asyncpg://app@localhost/agents?target_session_attrs=read-write",
            "postgresql://app@localhost/agents?target_session_attrs=read-write",
        ),
    ],
)
def test_the_app_database_url_becomes_a_libpq_connection_string(
    url: str, expected: str
) -> None:
    """The checkpointer shares the app's database, so it derives its own
    connection string from DATABASE_URL instead of a second setting."""
    assert psycopg_conninfo(url) == expected


@pytest.mark.parametrize(
    "query",
    [
        "prepared_statement_cache_size=0",
        "command_timeout=30",
        "async_fallback=false",
        "prepared_statement_cache_size=0&command_timeout=30&ssl=require",
    ],
)
def test_options_of_the_apps_own_engine_are_left_out(query: str) -> None:
    """These work in DATABASE_URL for the app's asyncpg engine, and libpq
    refuses them ("invalid URI query parameter"), so a URL the app runs on
    would stop the app from booting and revision 007 from migrating. They
    tune the engine's statement cache and its own timeouts; the
    checkpointer's pool prepares no statements and has its own timeouts."""
    conninfo = psycopg_conninfo(f"postgresql+asyncpg://app@localhost/agents?{query}")

    assert conninfo.split("?")[0] == "postgresql://app@localhost/agents"
    for option in (
        "prepared_statement_cache_size",
        "command_timeout",
        "async_fallback",
    ):
        assert option not in conninfo
    if "ssl=require" in query:
        assert conninfo.endswith("?sslmode=require")


@pytest.mark.parametrize(
    ("query", "named"),
    [
        ("direct_tls=true", "direct_tls"),
        (
            "statement_cache_size=0&server_settings=x",
            "server_settings, statement_cache_size",
        ),
        # Not an asyncpg ssl value either: the app's own engine refuses it.
        ("ssl=true", "ssl=true"),
    ],
)
def test_a_url_option_the_checkpointer_cannot_use_is_refused_by_name(
    query: str, named: str
) -> None:
    """Refused before connecting: inside the pool, libpq's error would only
    surface as a pool that never fills, after the whole timeout."""
    with pytest.raises(ValueError, match="DATABASE_URL") as refused:
        psycopg_conninfo(f"postgresql+asyncpg://app:s3cret@localhost/agents?{query}")

    assert named in str(refused.value)
    assert "s3cret" not in str(refused.value)


def test_a_non_postgres_url_is_refused() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        psycopg_conninfo("sqlite+aiosqlite:///local.db")


# ---------------------------------------------------------------------------
# Connection options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout_s", "connect_timeout", "tcp_user_timeout", "statement_timeout"),
    [(5.0, 5, 5_000, 5_000), (2.5, 3, 2_500, 2_500), (0.5, 2, 1_000, 500)],
)
def test_a_connection_gives_up_on_a_server_that_stops_answering(
    timeout_s: float,
    connect_timeout: int,
    tcp_user_timeout: int,
    statement_timeout: int,
) -> None:
    """``statement_timeout`` is enforced by the server, so it cannot end a
    wait on a server that is gone. ``connect_timeout`` bounds opening a
    connection (libpq's minimum is 2 seconds) and ``tcp_user_timeout`` drops
    a connection whose data the peer stopped acknowledging, such as the
    background purge's across a network partition."""
    kwargs = _connection_kwargs(timeout_s)

    assert kwargs["connect_timeout"] == connect_timeout
    assert kwargs["tcp_user_timeout"] == tcp_user_timeout
    assert kwargs["options"] == f"-c statement_timeout={statement_timeout}"
    assert kwargs["autocommit"] is True
    assert kwargs["prepare_threshold"] == 0


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
# What the app refuses to boot on
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetchone(self) -> dict[str, Any]:
        return self._row


class _Database:
    """Just enough of a psycopg pool to answer the boot check, from what the
    database would report: the schema version, the privileges the role
    lacks, and whether its transactions are read-only. The SQL itself is
    proven against a real PostgreSQL in ``test_checkpointer_integration.py``.
    """

    def __init__(
        self,
        *,
        version: int | None = CHECKPOINT_SCHEMA_VERSION,
        lacks: tuple[str, ...] = (),
        read_only: bool = False,
    ) -> None:
        self.version = version
        self.lacks = list(lacks)
        self.read_only = read_only

    def connection(self) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _connection() -> Any:
            yield self

        return _connection()

    async def execute(self, sql: str, params: Any = None) -> _Row:
        if "to_regclass" in sql:
            return _Row({"present": self.version is not None})
        if "checkpoint_migrations" in sql:
            return _Row({"v": self.version})
        if "has_table_privilege" in sql:
            return _Row({"missing": self.lacks, "read_only": self.read_only})
        raise AssertionError(f"unexpected query: {sql}")


async def test_a_role_with_every_privilege_on_the_current_schema_boots() -> None:
    await verify_checkpoint_schema(_Database())  # type: ignore[arg-type]


@pytest.mark.parametrize("version", [None, CHECKPOINT_SCHEMA_VERSION - 1])
async def test_a_missing_or_older_schema_is_refused(version: int | None) -> None:
    with pytest.raises(CheckpointSchemaError, match="alembic upgrade head"):
        await verify_checkpoint_schema(_Database(version=version))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "lacks",
    [
        ("DELETE on checkpoints",),
        ("INSERT on checkpoint_blobs", "UPDATE on checkpoint_blobs"),
    ],
)
async def test_a_role_that_cannot_save_or_purge_threads_is_refused(
    lacks: tuple[str, ...],
) -> None:
    """A role that can only read the tables would boot and then forget every
    conversation (each save fails) and never delete one (each purge fails):
    retention silently breached. The app refuses to start instead, naming
    what is missing."""
    with pytest.raises(CheckpointSchemaError) as refused:
        await verify_checkpoint_schema(_Database(lacks=lacks))  # type: ignore[arg-type]

    for missing in lacks:
        assert missing in str(refused.value)
    assert "SELECT, INSERT, UPDATE and DELETE" in str(refused.value)


async def test_a_read_only_connection_is_refused() -> None:
    """`default_transaction_read_only` on the role or the database, or a
    standby server: every save and every purge would fail."""
    with pytest.raises(CheckpointSchemaError, match="read-only"):
        await verify_checkpoint_schema(_Database(read_only=True))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CheckpointPurger
# ---------------------------------------------------------------------------


class _RecordingPurge:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[int | None] = []
        self.failures = failures
        self.ran = asyncio.Event()

    async def __call__(self, *, retention_s: int | None) -> PurgeResult:
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


async def test_every_purge_is_counted_by_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A purge that keeps failing (a role that cannot delete, a database that
    is gone) means conversations outlive their retention. That must be
    visible on /metrics, not only in a warning line per interval."""
    from prometheus_client import CollectorRegistry

    from agents_system.observability.metrics import build_metrics

    metrics = build_metrics(CollectorRegistry())
    purge = _RecordingPurge(failures=2)
    purger = CheckpointPurger(
        purge, retention_s=86_400, interval_s=0.01, metrics=metrics
    )

    await purger.start()
    await asyncio.wait_for(purge.ran.wait(), timeout=2.0)
    await purger.stop()

    def purges(outcome: str) -> float | None:
        return metrics.registry.get_sample_value(
            "agent_checkpoint_purges_total", {"outcome": outcome}
        )

    assert purges("error") == 2
    assert (purges("ok") or 0) >= 1
    # The warning says how long retention has not been enforced.
    assert _logged(capsys.readouterr().out, "consecutive_failures", 2)


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

    async def purge(*, retention_s: int | None) -> Any:
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


def test_the_dry_run_of_revision_007_prints_the_savers_own_migrations() -> None:
    """`alembic upgrade head --sql` is the documented dry run
    (docs/operations/deployment.md). Revision 007 runs the saver's own
    migrations on a live database; offline it must print the same
    statements, outside a transaction, because some build indexes
    concurrently."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from langgraph.checkpoint.postgres.base import BasePostgresSaver

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "006:007", "--sql"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+asyncpg://dry:run@localhost:5432/dry",
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    sql = result.stdout
    for statement in BasePostgresSaver.MIGRATIONS:
        assert statement.strip() in sql
    assert "INSERT INTO checkpoint_migrations (v) VALUES (9)" in sql
    concurrent = sql.index("CREATE INDEX CONCURRENTLY")
    assert sql.rfind("COMMIT;", 0, concurrent) > sql.rfind("BEGIN;", 0, concurrent)


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConnection:
    """Just enough of a psycopg AsyncConnection to record the purge's SQL."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def transaction(self) -> Any:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _transaction() -> Any:
            yield

        return _transaction()

    async def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.statements.append((sql, params))
        if "HAVING" in sql:
            return _FakeCursor([{"thread_id": "+5491100000000"}], 1)
        return _FakeCursor([], 2)


async def test_the_purger_drives_the_real_purge_with_the_configured_retention() -> None:
    """The wiring the lifespan uses: `partial(purge_checkpoints, pool)`."""
    from functools import partial

    from agents_system.services.checkpointer import purge_checkpoints

    conn = _FakeConnection()
    purger = CheckpointPurger(
        partial(purge_checkpoints, conn), retention_s=60, interval_s=3_600
    )

    await purger.start()
    for _ in range(100):
        if conn.statements:
            break
        await asyncio.sleep(0.01)
    await purger.stop()

    idle_queries = [params for sql, params in conn.statements if "HAVING" in sql]
    assert idle_queries == [{"retention_s": 60}]
