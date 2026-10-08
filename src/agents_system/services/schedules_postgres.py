"""Postgres schedules (ADR-011 part 3, #175).

One per-user table, ``subject_schedules``, keyed by (``runtime_id``,
``subject_key``, ``id``) and fenced by row-level security. Create, list and
delete go through the scope handle, so they only ever see one subject's rows.
The runner must claim whichever schedule is due, across subjects, so its
transactions set ``app.schedule_worker = 'on'``, the one setting the policy
lets through besides the subject pair.

A claim (``FOR UPDATE SKIP LOCKED``) also stores the schedule's next window in
the same transaction, so a window runs at most once across workers and
restarts. The table holds the user's request in their words and no address.

``PostgresScheduleStore`` is the `SubjectDataStore` (operator export, delete).
An unbound instance is registered at import so the migration's table has a
store; the served app binds it to its database at boot.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, time
from types import TracebackType
from typing import Any

from agents_system.extras import raise_missing_extra
from agents_system.schedules import (
    SCHEDULES_STORE,
    SCHEDULES_TABLE,
    ClaimedRun,
    Recurrence,
    Schedule,
    export_row,
)
from agents_system.services.scope_postgres import (
    PostgresScope,
    PostgresScopeProvider,
    subject_policy_statements,
)
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres schedule store", exc)

WORKER_SETTING = "app.schedule_worker"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_COLUMNS = (
    "id, runtime_id, subject_key, prompt, frequency, time_of_day, weekday, "
    "interval_s, tz, next_run_at, created_at, last_run_at, last_status"
)


def schedule_table_statements(table: str = SCHEDULES_TABLE) -> list[str]:
    """DDL for the schedules table and its fence (migration and tests)."""
    if not _IDENTIFIER.match(table):
        raise ValueError(f"not a plain table identifier: {table!r}")
    return [
        f"""CREATE TABLE {table} (
            id text NOT NULL,
            runtime_id text NOT NULL,
            subject_key text NOT NULL,
            prompt text NOT NULL,
            frequency text NOT NULL
                CHECK (frequency IN ('daily','weekly','interval')),
            time_of_day time,
            weekday smallint CHECK (weekday BETWEEN 0 AND 6),
            interval_s integer CHECK (interval_s > 0),
            tz text NOT NULL,
            next_run_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL,
            last_run_at timestamptz,
            last_status text,
            PRIMARY KEY (runtime_id, subject_key, id)
        )""",
        f"CREATE INDEX ix_{table}_due ON {table} (next_run_at, id)",
        *subject_policy_statements(table, open_setting=WORKER_SETTING),
    ]


def _to_schedule(row: Any) -> Schedule:
    return Schedule(
        id=row.id,
        runtime_id=row.runtime_id,
        subject_key=row.subject_key,
        prompt=row.prompt,
        recurrence=Recurrence(
            row.frequency,
            time_of_day=row.time_of_day.replace(tzinfo=None)
            if isinstance(row.time_of_day, time)
            else None,
            weekday=row.weekday,
            interval_s=row.interval_s,
            tz=row.tz,
        ),
        next_run_at=row.next_run_at,
        created_at=row.created_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
    )


class PostgresScheduleBackend:
    """One subject's schedules over a `PostgresScope`."""

    def __init__(self, scope: PostgresScope, *, table: str = SCHEDULES_TABLE) -> None:
        if not _IDENTIFIER.match(table):
            raise ValueError(f"not a plain table identifier: {table!r}")
        self._scope = scope
        self._table = table

    async def list(self) -> list[Schedule]:
        async with self._scope.transaction() as conn:
            rows = await conn.execute(
                text(f"SELECT {_COLUMNS} FROM {self._table} ORDER BY created_at, id")
            )
            return [_to_schedule(r) for r in rows]

    async def add(self, schedule: Schedule, *, max_per_user: int) -> bool:
        rec = schedule.recurrence
        async with self._scope.transaction() as conn:
            # Serializes this subject's creates so the cap cannot be overshot.
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {
                    "k": f"{SCHEDULES_STORE}:{schedule.runtime_id}:{schedule.subject_key}"
                },
            )
            held = (
                await conn.execute(text(f"SELECT count(*) FROM {self._table}"))
            ).scalar_one()
            if held >= max_per_user:
                return False
            await conn.execute(
                text(
                    f"INSERT INTO {self._table} ({_COLUMNS}) VALUES "
                    "(:id, :r, :s, :p, :f, :t, :w, :i, :tz, :n, :c, NULL, NULL)"
                ),
                {
                    "id": schedule.id,
                    "r": schedule.runtime_id,
                    "s": schedule.subject_key,
                    "p": schedule.prompt,
                    "f": rec.frequency,
                    "t": rec.time_of_day,
                    "w": rec.weekday,
                    "i": rec.interval_s,
                    "tz": rec.tz,
                    "n": schedule.next_run_at,
                    "c": schedule.created_at,
                },
            )
            return True

    async def remove(self, schedule_id: str) -> bool:
        async with self._scope.transaction() as conn:
            result = await conn.execute(
                text(f"DELETE FROM {self._table} WHERE id = :id"), {"id": schedule_id}
            )
            return bool(result.rowcount)


class _WorkerTx:
    """A transaction with the worker setting on (transaction-local)."""

    def __init__(self, provider: PostgresScopeProvider) -> None:
        self._cm = provider.engine.begin()

    async def __aenter__(self) -> AsyncConnection:
        conn = await self._cm.__aenter__()
        await conn.execute(
            text("SELECT set_config(:k, 'on', true)"), {"k": WORKER_SETTING}
        )
        return conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return await self._cm.__aexit__(exc_type, exc, tb)


class PostgresScheduleStore:
    """The worker side and the registered store for ``subject_schedules``."""

    name = SCHEDULES_STORE

    def __init__(
        self,
        provider: PostgresScopeProvider | None = None,
        *,
        table: str = SCHEDULES_TABLE,
    ) -> None:
        if not _IDENTIFIER.match(table):
            raise ValueError(f"not a plain table identifier: {table!r}")
        self._provider = provider
        self._table = table
        self.tables = frozenset({table})

    @property
    def unbound(self) -> bool:
        """True for the import-time placeholder that holds no provider."""
        return self._provider is None

    def _require(self) -> PostgresScopeProvider:
        if self._provider is None:
            raise RuntimeError("the schedule store is not bound to a database")
        return self._provider

    # -- ScheduleWorkerStore ----------------------------------------------

    async def claim_due(
        self, now: datetime, advance: Callable[[Schedule, datetime], datetime]
    ) -> ClaimedRun | None:
        async with _WorkerTx(self._require()) as conn:
            row = (
                await conn.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM {self._table} "
                        "WHERE next_run_at <= :now ORDER BY next_run_at, id "
                        "FOR UPDATE SKIP LOCKED LIMIT 1"
                    ),
                    {"now": now},
                )
            ).first()
            if row is None:
                return None
            schedule = _to_schedule(row)
            following = advance(schedule, now)
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET next_run_at = :n "
                    "WHERE runtime_id = :r AND subject_key = :s AND id = :id"
                ),
                {
                    "n": following,
                    "r": schedule.runtime_id,
                    "s": schedule.subject_key,
                    "id": schedule.id,
                },
            )
        return ClaimedRun(
            Schedule(**{**schedule.__dict__, "next_run_at": following}),
            schedule.next_run_at,
        )

    async def record(
        self,
        runtime_id: str,
        subject_key: str,
        schedule_id: str,
        status: str,
        now: datetime,
    ) -> None:
        async with _WorkerTx(self._require()) as conn:
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET last_run_at = :now, "
                    "last_status = :st WHERE runtime_id = :r AND subject_key = :s "
                    "AND id = :id"
                ),
                {
                    "now": now,
                    "st": status,
                    "r": runtime_id,
                    "s": subject_key,
                    "id": schedule_id,
                },
            )

    # -- SubjectDataStore --------------------------------------------------

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        if self._provider is None:
            return []
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        backend = PostgresScheduleBackend(scope, table=self._table)
        return [export_row(s) for s in await backend.list()]

    async def delete(self, subject: SubjectRef) -> int:
        if self._provider is None:
            return 0
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        async with scope.transaction() as conn:
            result = await conn.execute(text(f"DELETE FROM {self._table}"))
            return int(result.rowcount)


#: The unbound placeholder, registered so the migration's table has a store.
SCHEDULE_STORE_PLACEHOLDER = PostgresScheduleStore()
if SCHEDULES_STORE not in DEFAULT_REGISTRY.stores:
    DEFAULT_REGISTRY.register(SCHEDULE_STORE_PLACEHOLDER)
