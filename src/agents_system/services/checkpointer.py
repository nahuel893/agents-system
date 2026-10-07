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
  the installed saver needs, or that its role cannot write and delete in.
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
import math
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from agents_system.extras import raise_missing_extra
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    Metrics,
    record_checkpoint_purge,
)

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


#: Options of the app's own asyncpg engine, valid in ``DATABASE_URL`` but not
#: libpq connection parameters: its statement cache, its statement timeout,
#: and SQLAlchemy's pool fallback. The checkpointer's pool prepares no
#: statements and has its own timeouts (``CHECKPOINTER_TIMEOUT_S``), so they
#: do not apply to it and are left out.
_ENGINE_ONLY_OPTIONS = frozenset(
    {"prepared_statement_cache_size", "command_timeout", "async_fallback"}
)

#: libpq's ``sslmode`` values, which asyncpg's ``ssl`` accepts too.
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def _libpq_parameters() -> frozenset[str]:
    """Every connection parameter the installed libpq accepts."""
    from psycopg import pq

    return frozenset(option.keyword.decode() for option in pq.Conninfo.get_defaults())


def psycopg_conninfo(database_url: str) -> str:
    """Turn the app's SQLAlchemy ``DATABASE_URL`` into a libpq connection
    string for psycopg.

    The checkpointer shares the app's database, so it derives its connection
    from the same setting rather than a second one that could drift. The
    driver changes, asyncpg's ``ssl`` query parameter becomes libpq's
    ``sslmode``, and the options of the app's own engine are left out (see
    ``_ENGINE_ONLY_OPTIONS``). Any other query parameter libpq does not accept
    is refused by name, here, rather than as a connection pool that never
    fills.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError(
            f"the checkpointer needs a PostgreSQL DATABASE_URL, got {url.drivername!r}"
        )
    query = {k: v for k, v in url.query.items() if k not in _ENGINE_ONLY_OPTIONS}
    if "ssl" in query and "sslmode" not in query:
        ssl = query.pop("ssl")
        mode = ssl.replace("_", "-") if isinstance(ssl, str) else None
        if mode not in _SSL_MODES:
            raise ValueError(
                f"DATABASE_URL has ssl={ssl}; the checkpointer, like the app's "
                f"asyncpg engine, needs one of: {', '.join(_SSL_MODES)}"
            )
        query["sslmode"] = mode
    unknown = sorted(set(query) - _libpq_parameters())
    if unknown:
        raise ValueError(
            f"DATABASE_URL has {', '.join(unknown)}, which the checkpointer's "
            "PostgreSQL connection (libpq) does not accept: remove it, or see "
            "docs/platform/conversation-memory.md for the supported parameters"
        )
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


#: The tables every turn writes and every purge deletes from, and what the
#: app's role needs on each: reading and saving a thread upserts, retention
#: deletes. ``checkpoint_migrations`` is only read, by the version check.
_THREAD_TABLES = ("checkpoint_blobs", "checkpoint_writes", "checkpoints")
_THREAD_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")

# One privilege per `has_table_privilege` call: given a list such as
# 'INSERT,DELETE' it answers whether ANY of them is held, not all.
# `transaction_read_only` is on under `default_transaction_read_only` (for
# the role or the database) and on a standby server.
_ACCESS = """
SELECT current_setting('transaction_read_only') = 'on' AS read_only,
       ARRAY(
           SELECT p || ' on ' || t
           FROM unnest(%(tables)s::text[]) AS t,
                unnest(%(privileges)s::text[]) AS p
           WHERE NOT has_table_privilege(t, p)
           ORDER BY t, p
       ) AS missing
"""


async def verify_checkpoint_schema(pool: AsyncConnectionPool[Any]) -> None:
    """Refuse a database the checkpointer cannot keep threads in.

    The app never creates tables at boot: a missing or outdated schema means
    the deployment's migration step did not run, and running turns against it
    would degrade every one of them.

    The tables can also exist and still be unusable: the migration role
    creates them, and the app may connect as a different one. A role that
    can read them but not write or delete, or a read-only connection, would
    boot, then forget every conversation (each save fails) and never delete
    one (each purge fails) -- a silent retention breach. It is refused too.
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
        cursor = await conn.execute(
            _ACCESS,
            {"tables": list(_THREAD_TABLES), "privileges": list(_THREAD_PRIVILEGES)},
        )
        access = await cursor.fetchone()
    if access is None:  # a SELECT without FROM always returns its one row
        raise CheckpointSchemaError("could not read the database role's privileges")
    if access["missing"]:
        raise CheckpointSchemaError(
            "the database role cannot keep conversation threads: it lacks "
            f"{', '.join(access['missing'])}. Grant it SELECT, INSERT, UPDATE "
            f"and DELETE on {', '.join(_THREAD_TABLES)}"
        )
    if access["read_only"]:
        raise CheckpointSchemaError(
            "the database connection is read-only (default_transaction_read_only, "
            "or a standby server), so no conversation thread could be saved or "
            "purged: point DATABASE_URL at a writable primary"
        )


def _connection_kwargs(timeout_s: float) -> dict[str, Any]:
    """The options of every connection in the checkpointer's pool.

    Autocommit, and no server-side prepared statements, as the saver
    requires. ``statement_timeout`` ends a slow statement, but the server
    enforces it, so it cannot end a wait on a server that is gone:
    ``connect_timeout`` bounds opening a connection (libpq's minimum is two
    whole seconds) and ``tcp_user_timeout`` drops a connection whose data
    the peer stopped acknowledging, as across a network partition -- which
    also frees the background purge, whose statements have no timeout.
    """
    from psycopg.rows import dict_row

    return {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": f"-c statement_timeout={int(timeout_s * 1000)}",
        "connect_timeout": max(2, math.ceil(timeout_s)),
        "tcp_user_timeout": max(1_000, int(timeout_s * 1000)),
    }


@asynccontextmanager
async def open_postgres_checkpointer(
    database_url: str, *, pool_size: int, timeout_s: float
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open an ``AsyncPostgresSaver`` on its own connection pool.

    A pool, not the single connection ``from_conn_string`` holds: turns save
    concurrently, and a broken connection (a database restart, a network
    drop) is replaced on the next checkout instead of failing every later
    turn. ``timeout_s`` bounds waiting for a connection, opening one, and
    each statement (see ``_connection_kwargs``). A server that stops
    answering altogether is cut off on the client, by the runtime's own
    bound on each read and save (``AgentRuntime(checkpointer_timeout_s=)``).
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=psycopg_conninfo(database_url),
        min_size=1,
        max_size=pool_size,
        timeout=timeout_s,
        check=AsyncConnectionPool.check_connection,
        kwargs=_connection_kwargs(timeout_s),
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

    Every run is counted by outcome (``agent_checkpoint_purges_total``) and a
    failure logs how many runs in a row have failed: a purge that keeps
    failing means conversations outlive their retention, which must be
    visible and alertable, not only a warning line per interval.
    """

    def __init__(
        self,
        purge: PurgeFn,
        *,
        retention_s: int | None,
        interval_s: float,
        metrics: Metrics | None = None,
    ) -> None:
        self._purge = purge
        self._retention_s = retention_s
        self._interval_s = interval_s
        self._metrics = metrics if metrics is not None else DEFAULT_METRICS
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
        consecutive_failures = 0
        while True:
            try:
                result = await self._purge(retention_s=self._retention_s)
            except Exception as exc:
                consecutive_failures += 1
                record_checkpoint_purge(self._metrics, outcome="error")
                logger.warning(
                    "checkpointer.purge_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    consecutive_failures=consecutive_failures,
                    retention_s=self._retention_s,
                )
            else:
                consecutive_failures = 0
                record_checkpoint_purge(self._metrics, outcome="ok")
                logger.info(
                    "checkpointer.purged",
                    threads_deleted=result.threads_deleted,
                    checkpoints_pruned=result.checkpoints_pruned,
                    retention_s=self._retention_s,
                )
            await asyncio.sleep(self._interval_s)


class CheckpointThreads:
    """Per-thread export and delete over any LangGraph saver (ADR-011 part 6).

    The purger only removes idle threads by age; this removes one thread on
    demand, which the operator's ``delete_subject`` needs. Postgres and the
    in-memory saver both implement ``adelete_thread``, so no SQL lives here.
    """

    def __init__(self, saver: Any) -> None:
        self._saver = saver

    async def export_thread(self, thread_id: str) -> dict[str, Any] | None:
        from langchain_core.messages import messages_to_dict

        found = await self._saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        if found is None:
            return None
        messages = found.checkpoint.get("channel_values", {}).get("messages", [])
        return {"thread_id": thread_id, "messages": messages_to_dict(messages)}

    async def delete_thread(self, thread_id: str) -> int:
        """Delete every checkpoint, write and blob of the thread; 1 if it existed."""
        existed = await self.export_thread(thread_id) is not None
        await self._saver.adelete_thread(thread_id)
        return 1 if existed else 0
