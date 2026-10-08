"""Postgres proactive queue (ADR-011 part 5, #251).

One per-user table, ``proactive_outbox``, keyed by (``runtime_id``,
``subject_key``, ``id``) and fenced by row-level security. Enqueue, export and
delete go through the scope handle, so they only ever see one subject's rows.
The dispatcher must claim whichever message is due, across subjects, so its
transactions set ``app.proactive_worker = 'on'``, the one setting the policy
lets through besides the subject pair.

The lease discipline is the webhook outbox's: ``FOR UPDATE SKIP LOCKED``
hands a message to one worker, and a message whose lease lapsed mid-send is
marked ``ambiguous``, never requeued (delivery is at most once). The table
holds no address: the directory resolves it at send time.

``PROACTIVE_STORE`` is the `SubjectDataStore` registered in the default
registry at import, so the guard that every ``subject_key`` table has a store
passes. It is bound to a database by constructing ``PostgresProactiveQueue``
over a provider (default table), and by the served app at boot when
``create_app(scope_provider_factory=...)`` is configured. Unbound, it holds
nothing, so export and delete are no-ops.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any

from agents_system.extras import raise_missing_extra
from agents_system.proactive import MessageStatus, ProactiveMessage
from agents_system.services.scope_postgres import (
    PostgresScopeProvider,
    subject_policy_statements,
)
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres proactive queue", exc)

TABLE = "proactive_outbox"
WORKER_SETTING = "app.proactive_worker"
TERMINAL = ("sent", "failed", "ambiguous")

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def proactive_table_statements(table: str = TABLE) -> list[str]:
    """DDL for the queue table and its fence (used by the migration and tests)."""
    if not _IDENTIFIER.match(table):
        raise ValueError(f"not a plain table identifier: {table!r}")
    return [
        f"""CREATE TABLE {table} (
            id uuid NOT NULL,
            runtime_id text NOT NULL,
            subject_key text NOT NULL,
            body text NOT NULL,
            actor text NOT NULL,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','sending','sent','failed','ambiguous')),
            available_at timestamptz NOT NULL,
            lease_owner text,
            lease_expires_at timestamptz,
            claimed_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            PRIMARY KEY (runtime_id, subject_key, id)
        )""",
        (
            f"CREATE INDEX ix_{table}_due ON {table} (available_at, id) "
            "WHERE status = 'queued'"
        ),
        (
            f"CREATE INDEX ix_{table}_attempts ON {table} (runtime_id, claimed_at) "
            "WHERE claimed_at IS NOT NULL"
        ),
        *subject_policy_statements(table, open_setting=WORKER_SETTING),
    ]


class PostgresProactiveQueue:
    """The queue and the registered store for ``proactive_outbox``."""

    name = "proactive"

    def __init__(
        self, provider: PostgresScopeProvider | None = None, *, table: str = TABLE
    ) -> None:
        if not _IDENTIFIER.match(table):
            raise ValueError(f"not a plain table identifier: {table!r}")
        self._provider = provider
        self._table = table
        self.tables = frozenset({table})
        # The queue a deployment builds to deliver is the one operator export,
        # delete and retention must reach: bind the registered store to the
        # same database so the two cannot drift apart (#251).
        if provider is not None and table == TABLE:
            PROACTIVE_STORE.bind(provider)

    def bind(self, provider: PostgresScopeProvider) -> None:
        self._provider = provider

    def _require(self) -> PostgresScopeProvider:
        if self._provider is None:
            raise RuntimeError("the proactive queue is not bound to a database")
        return self._provider

    def _worker_tx(self) -> _WorkerTx:
        return _WorkerTx(self._require())

    # -- ProactiveQueue ----------------------------------------------------

    async def enqueue(
        self,
        runtime_id: str,
        subject_key: str,
        text_: str,
        *,
        actor: str,
        available_at: datetime,
    ) -> str:
        message_id = uuid.uuid4()
        async with self._require().scope(runtime_id, subject_key).transaction() as c:
            await c.execute(
                text(
                    f"INSERT INTO {self._table} "
                    "(id, runtime_id, subject_key, body, actor, available_at) "
                    "VALUES (:id, :r, :s, :b, :a, :at)"
                ),
                {
                    "id": message_id,
                    "r": runtime_id,
                    "s": subject_key,
                    "b": text_,
                    "a": actor,
                    "at": available_at,
                },
            )
        return str(message_id)

    async def expire_leases(self, now: datetime) -> list[ProactiveMessage]:
        async with self._worker_tx() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"UPDATE {self._table} SET status = 'ambiguous', "
                        "last_error = 'lease_expired', lease_owner = NULL, "
                        "lease_expires_at = NULL, finished_at = :now "
                        "WHERE status = 'sending' AND lease_expires_at <= :now "
                        "RETURNING id, runtime_id, subject_key, body, actor, "
                        "available_at"
                    ),
                    {"now": now},
                )
            ).all()
        return [
            ProactiveMessage(
                id=str(r.id),
                runtime_id=r.runtime_id,
                subject_key=r.subject_key,
                text=r.body,
                actor=r.actor,
                status="ambiguous",
                available_at=r.available_at,
                last_error="lease_expired",
            )
            for r in rows
        ]

    async def claim(
        self, worker_id: str, *, now: datetime, lease_s: float
    ) -> ProactiveMessage | None:
        await self.expire_leases(now)
        async with self._worker_tx() as conn:
            row = (
                await conn.execute(
                    text(
                        f"UPDATE {self._table} SET status = 'sending', "
                        "lease_owner = :w, lease_expires_at = :exp, claimed_at = :now "
                        f"WHERE id = (SELECT id FROM {self._table} "
                        "WHERE status = 'queued' AND available_at <= :now "
                        "ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
                        "RETURNING id, runtime_id, subject_key, body, actor, "
                        "available_at, lease_expires_at"
                    ),
                    {
                        "w": worker_id,
                        "now": now,
                        "exp": now + timedelta(seconds=lease_s),
                    },
                )
            ).first()
        if row is None:
            return None
        return ProactiveMessage(
            id=str(row.id),
            runtime_id=row.runtime_id,
            subject_key=row.subject_key,
            text=row.body,
            actor=row.actor,
            status="sending",
            available_at=row.available_at,
            lease_owner=worker_id,
            lease_expires_at=row.lease_expires_at,
        )

    async def attempts_since(
        self,
        runtime_id: str,
        subject_key: str | None,
        since: datetime,
        *,
        exclude: str | None = None,
    ) -> int:
        async with self._worker_tx() as conn:
            count = (
                await conn.execute(
                    text(
                        f"SELECT count(*) FROM {self._table} "
                        "WHERE runtime_id = :r AND claimed_at >= :since "
                        "AND status IN ('sending','sent','ambiguous') "
                        "AND (CAST(:s AS text) IS NULL OR subject_key = :s) "
                        "AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))"
                    ),
                    {"r": runtime_id, "since": since, "s": subject_key, "x": exclude},
                )
            ).scalar_one()
        return int(count)

    async def finish(
        self, message_id: str, worker_id: str, status: MessageStatus, error: str | None
    ) -> None:
        async with self._worker_tx() as conn:
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET status = :st, last_error = :e, "
                    "lease_owner = NULL, lease_expires_at = NULL, finished_at = now() "
                    "WHERE id = :id AND lease_owner = :w"
                ),
                {"st": status, "e": error, "id": uuid.UUID(message_id), "w": worker_id},
            )

    async def defer(self, message_id: str, worker_id: str, until: datetime) -> None:
        async with self._worker_tx() as conn:
            await conn.execute(
                text(
                    f"UPDATE {self._table} SET status = 'queued', available_at = :at, "
                    "lease_owner = NULL, lease_expires_at = NULL, claimed_at = NULL "
                    "WHERE id = :id AND lease_owner = :w"
                ),
                {"at": until, "id": uuid.UUID(message_id), "w": worker_id},
            )

    # -- SubjectDataStore --------------------------------------------------

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        if self._provider is None:
            return []
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        async with scope.transaction() as conn:
            rows = await conn.execute(
                text(
                    f"SELECT id, body, actor, status, created_at FROM {self._table} "
                    "ORDER BY created_at, id"
                )
            )
            return [
                {
                    "id": str(r.id),
                    "body": r.body,
                    "actor": r.actor,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    async def delete(self, subject: SubjectRef) -> int:
        if self._provider is None:
            return 0
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        async with scope.transaction() as conn:
            return (await conn.execute(text(f"DELETE FROM {self._table}"))).rowcount

    async def purge(self, *, retention_s: int) -> int:
        """Delete finished messages older than the retention; queued ones stay."""
        if self._provider is None:
            return 0
        async with self._worker_tx() as conn:
            return (
                await conn.execute(
                    text(
                        f"DELETE FROM {self._table} WHERE status IN "
                        "('sent','failed','ambiguous') "
                        "AND finished_at < now() - make_interval(secs => :s)"
                    ),
                    {"s": retention_s},
                )
            ).rowcount


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


#: Registered at import so the "every subject table has a store" guard holds.
PROACTIVE_STORE = PostgresProactiveQueue()
DEFAULT_REGISTRY.register(PROACTIVE_STORE)
