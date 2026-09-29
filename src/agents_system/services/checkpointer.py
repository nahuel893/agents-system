"""Durable conversation threads in PostgreSQL (issue #24).

The served app keeps each conversation thread with LangGraph's
``AsyncPostgresSaver`` (``langgraph-checkpoint-postgres``) in the platform's
own PostgreSQL database -- the store that already holds the durable webhook
inbox and outbox (Alembic revisions 002-004) and the audit log. A process
restart or a container recreation therefore no longer loses a conversation.

Three concerns live here, each separate from the others:

- **Durability** -- ``open_postgres_checkpointer``: a psycopg connection pool
  on the app's database, bounded by a timeout so a slow database degrades a
  turn (``agent/graph.py``'s ``_Thread``) instead of stalling it. It refuses
  to start on a database whose checkpoint schema is missing or older than
  the installed saver needs.
- **Schema** -- ``apply_checkpoint_migrations``: the saver's own versioned
  migrations, run by Alembic revision 007 during the deployment's migration
  step, so the tables are created where every other table is and by the same
  role, while their DDL stays owned by the library that reads them.
- **Retention** -- ``purge_checkpoints`` and ``CheckpointPurger``: a periodic
  job that deletes every thread idle for longer than the configured
  retention, and prunes the superseded checkpoints of every thread. The
  saver itself has no expiry; retention is only ever this job.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from agents_system.extras import raise_missing_extra

try:
    from sqlalchemy.engine import make_url
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres checkpointer", exc)

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

logger = structlog.get_logger()

#: The checkpoint schema version Alembic revision 007 was written against:
#: ``len(BasePostgresSaver.MIGRATIONS) - 1`` of the pinned
#: ``langgraph-checkpoint-postgres``. A test fails when an upgraded library
#: ships more migrations -- see ``tests/test_checkpointer_service.py``.
CHECKPOINT_SCHEMA_VERSION = 9

#: The tables the saver owns. Alembic's autogenerate must ignore them
#: (``alembic/env.py``): they are not in the ORM metadata by design.
CHECKPOINT_TABLES = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


class CheckpointSchemaError(RuntimeError):
    """The database does not hold the checkpoint schema the saver needs."""


def psycopg_conninfo(database_url: str) -> str:
    """Turn the app's SQLAlchemy ``DATABASE_URL`` into a libpq connection
    string for psycopg.

    The checkpointer shares the app's database, so it derives its connection
    from the same setting rather than a second one that could drift. Only the
    driver changes; asyncpg's ``ssl`` query parameter becomes libpq's
    ``sslmode``.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError(
            f"the checkpointer needs a PostgreSQL DATABASE_URL, got {url.drivername!r}"
        )
    query = dict(url.query)
    if "ssl" in query and "sslmode" not in query:
        query["sslmode"] = query.pop("ssl")
    return url.set(drivername="postgresql", query=query).render_as_string(
        hide_password=False
    )


def apply_checkpoint_migrations(database_url: str) -> None:
    """Bring the checkpoint schema to what the installed saver needs.

    Runs the saver's own ``setup()``: its migrations are versioned in its
    ``checkpoint_migrations`` table and each is applied once, so this is
    idempotent. Called by Alembic, outside Alembic's transaction (some of the
    saver's migrations build indexes concurrently).
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(psycopg_conninfo(database_url)) as saver:
        saver.setup()


async def verify_checkpoint_schema(pool: AsyncConnectionPool[Any]) -> None:
    """Refuse a database without the checkpoint schema the saver needs.

    The app never creates tables at boot: a missing or outdated schema means
    the deployment's migration step did not run, and running turns against it
    would degrade every one of them.
    """
    from langgraph.checkpoint.postgres.base import BasePostgresSaver

    needed = len(BasePostgresSaver.MIGRATIONS) - 1
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT to_regclass('checkpoint_migrations') IS NOT NULL AS present"
        )
        row = await cursor.fetchone()
        version = None
        if row is not None and row["present"]:
            cursor = await conn.execute("SELECT max(v) AS v FROM checkpoint_migrations")
            found = await cursor.fetchone()
            version = None if found is None else found["v"]
    if version is None or version < needed:
        raise CheckpointSchemaError(
            f"the checkpoint schema is at version {version}, the installed "
            f"checkpointer needs {needed}: run `alembic upgrade head`"
        )


@asynccontextmanager
async def open_postgres_checkpointer(
    database_url: str, *, pool_size: int, timeout_s: float
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open an ``AsyncPostgresSaver`` on its own connection pool.

    A pool, not the single connection ``from_conn_string`` holds: turns save
    concurrently, and a broken connection (a database restart, a network
    drop) is replaced on the next checkout instead of failing every later
    turn. ``timeout_s`` bounds both waiting for a connection and each
    statement. Connections use autocommit, as the saver requires.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=psycopg_conninfo(database_url),
        min_size=1,
        max_size=pool_size,
        timeout=timeout_s,
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c statement_timeout={int(timeout_s * 1000)}",
        },
        open=False,
    )
    await pool.open(wait=True, timeout=timeout_s)
    try:
        await verify_checkpoint_schema(pool)
        yield AsyncPostgresSaver(conn=pool)
    finally:
        await pool.close()


@dataclasses.dataclass(frozen=True)
class PurgeResult:
    """What one purge removed. Counts only: thread ids are phone numbers."""

    threads_deleted: int
    checkpoints_pruned: int


# Every thread whose most recent checkpoint is older than the cutoff. `ts` is
# the ISO-8601 time LangGraph stamps on each checkpoint it writes.
_IDLE_THREADS = """
SELECT thread_id FROM checkpoints
GROUP BY thread_id
HAVING max((checkpoint ->> 'ts')::timestamptz)
       < now() - make_interval(secs => %(retention_s)s)
"""

# A checkpoint saved while the purge runs is newer than the cutoff and
# survives; its thread is simply not idle any more.
_DELETE_IDLE_CHECKPOINTS = """
DELETE FROM checkpoints
WHERE thread_id = ANY(%(threads)s)
  AND (checkpoint ->> 'ts')::timestamptz
      < now() - make_interval(secs => %(retention_s)s)
"""
_DELETE_ORPHANED = """
DELETE FROM {table} t
WHERE t.thread_id = ANY(%(threads)s)
  AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id = t.thread_id)
"""

# Each thread keeps its latest checkpoint only: the platform never reads an
# earlier one (no time travel), and each holds a full copy of the history, so
# keeping them grows storage with the square of a conversation's length.
# Safe because the agent's state has no DeltaChannel, whose history spans
# checkpoints (see BaseCheckpointSaver.prune); a test pins that.
_PRUNE_CHECKPOINTS = """
DELETE FROM checkpoints c
USING (
    SELECT thread_id, checkpoint_ns, max(checkpoint_id) AS latest
    FROM checkpoints GROUP BY thread_id, checkpoint_ns
) l
WHERE c.thread_id = l.thread_id AND c.checkpoint_ns = l.checkpoint_ns
  AND c.checkpoint_id < l.latest
"""
_PRUNE_WRITES = """
DELETE FROM checkpoint_writes w
USING (
    SELECT thread_id, checkpoint_ns, max(checkpoint_id) AS latest
    FROM checkpoints GROUP BY thread_id, checkpoint_ns
) l
WHERE w.thread_id = l.thread_id AND w.checkpoint_ns = l.checkpoint_ns
  AND w.checkpoint_id < l.latest
"""
# A channel's blob versions only grow, so a version below the one the latest
# checkpoint references can never be read again. Comparing the zero-padded
# counter part only keeps a blob written by a concurrent save.
_PRUNE_BLOBS = """
DELETE FROM checkpoint_blobs b
USING checkpoints c,
      jsonb_each_text(c.checkpoint -> 'channel_versions') AS v(channel, version)
WHERE c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns
  AND c.checkpoint_id = (
      SELECT max(x.checkpoint_id) FROM checkpoints x
      WHERE x.thread_id = c.thread_id AND x.checkpoint_ns = c.checkpoint_ns
  )
  AND v.channel = b.channel
  AND split_part(b.version, '.', 1) < split_part(v.version, '.', 1)
"""


async def purge_checkpoints(
    conn: AsyncConnectionPool[Any] | AsyncConnection[Any],
    *,
    retention_s: int | None,
) -> PurgeResult:
    """Enforce retention, then prune superseded checkpoints.

    ``retention_s=None`` keeps idle threads indefinitely and only prunes.
    Runs in one transaction without the pool's per-statement timeout, which
    bounds the turn path, not this background job.
    """
    from psycopg_pool import AsyncConnectionPool

    if isinstance(conn, AsyncConnectionPool):
        async with conn.connection() as pooled:
            return await _purge(pooled, retention_s)
    return await _purge(conn, retention_s)


async def _purge(conn: AsyncConnection[Any], retention_s: int | None) -> PurgeResult:
    threads_deleted = 0
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = 0")
        if retention_s is not None:
            cursor = await conn.execute(_IDLE_THREADS, {"retention_s": retention_s})
            threads = [row["thread_id"] for row in await cursor.fetchall()]
            if threads:
                params = {"threads": threads, "retention_s": retention_s}
                await conn.execute(_DELETE_IDLE_CHECKPOINTS, params)
                for table in ("checkpoint_blobs", "checkpoint_writes"):
                    await conn.execute(_DELETE_ORPHANED.format(table=table), params)
                threads_deleted = len(threads)
        pruned = await conn.execute(_PRUNE_CHECKPOINTS)
        checkpoints_pruned = pruned.rowcount
        await conn.execute(_PRUNE_WRITES)
        await conn.execute(_PRUNE_BLOBS)
    return PurgeResult(
        threads_deleted=threads_deleted, checkpoints_pruned=checkpoints_pruned
    )


class PurgeFn(Protocol):
    """A purge bound to its store, such as ``partial(purge_checkpoints, pool)``."""

    def __call__(self, *, retention_s: int | None) -> Awaitable[PurgeResult]: ...


class CheckpointPurger:
    """Run a purge now and then every ``interval_s`` seconds until stopped.

    A failed purge is logged and retried at the next interval: retention is
    enforced eventually, never at the cost of the process. The retention a
    purge enforces is exactly ``retention_s``, whatever backend runs it.
    """

    def __init__(
        self, purge: PurgeFn, *, retention_s: int | None, interval_s: float
    ) -> None:
        self._purge = purge
        self._retention_s = retention_s
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="checkpoint-purger")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                result = await self._purge(retention_s=self._retention_s)
            except Exception as exc:
                logger.warning(
                    "checkpointer.purge_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            else:
                logger.info(
                    "checkpointer.purged",
                    threads_deleted=result.threads_deleted,
                    checkpoints_pruned=result.checkpoints_pruned,
                    retention_s=self._retention_s,
                )
            await asyncio.sleep(self._interval_s)
