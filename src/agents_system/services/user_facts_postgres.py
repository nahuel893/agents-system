"""Postgres store of per-user facts over the scope handle (#250, ADR-011).

Every statement runs in a ``PostgresScope`` transaction, so the row-level
security policy of the ``subject_facts`` migration fences it to one (runtime
id, subject key): the unfiltered queries here still see only that subject's
rows.

An unbound placeholder registers itself in the default `SubjectDataRegistry`
at import so the migration guard sees the table has a store. Each served app
builds its own `PostgresUserFactStore(provider)` and swaps it in for the
placeholder in its own copy of the registry (`SubjectDataRegistry.bind`).
Unbound, the store holds nothing and exports and deletes nothing.

Retention has two parts. An expired fact is never listed, injected or
exported, and its subject's next read or write deletes it. ``purge`` deletes
the expired facts of every subject, including those who never return, through
the maintenance path of migration 011 (a transaction that sets
``app.maintenance``, which opens only rows past ``expires_at``); the
``SubjectDataPurger`` runs it on its interval. Each remember-and-evict runs
in one transaction holding a per-subject advisory lock, so concurrent writers
of one subject cannot erase each other's facts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from agents_system.extras import raise_missing_extra
from agents_system.services.scope_postgres import (
    PURGE_EXPIRED,
    PostgresScope,
    PostgresScopeProvider,
)
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef
from agents_system.user_memory import (
    USER_FACTS_STORE,
    USER_FACTS_TABLE,
    UserFact,
    export_row,
)

try:
    from sqlalchemy import text
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres user-fact store", exc)

__all__ = [
    "USER_FACTS_STORE_PLACEHOLDER",
    "PostgresFactBackend",
    "PostgresScope",
    "PostgresUserFactStore",
]

_COLUMNS = "id, content, created_at, last_used_at, uses, expires_at"


def _fact(row: Any) -> UserFact:
    return UserFact(**dict(row._mapping))


class PostgresFactBackend:
    """One subject's facts, in transactions of its own scope."""

    def __init__(self, scope: PostgresScope) -> None:
        self._scope = scope
        self._held: Any = None

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Any]:
        # Inside `serialized` every statement joins the one held transaction.
        if self._held is not None:
            yield self._held
            return
        async with self._scope.transaction() as conn:
            yield conn

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        async with self._scope.transaction() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"{self._scope.runtime_id}\x1f{self._scope.subject_key}"},
            )
            self._held = conn
            try:
                yield
            finally:
                self._held = None

    async def all(self) -> list[UserFact]:
        # No subject predicate on purpose: the policy is what fences the read.
        async with self._transaction() as conn:
            rows = await conn.execute(
                text(f"SELECT {_COLUMNS} FROM {USER_FACTS_TABLE}")
            )
            return [_fact(r) for r in rows]

    async def put(self, fact: UserFact) -> None:
        async with self._transaction() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {USER_FACTS_TABLE} (runtime_id, subject_key, "
                    f"{_COLUMNS}) VALUES (:runtime_id, :subject_key, :id, :content, "
                    ":created_at, :last_used_at, :uses, :expires_at) "
                    "ON CONFLICT (runtime_id, subject_key, id) DO UPDATE SET "
                    "content = EXCLUDED.content, created_at = EXCLUDED.created_at, "
                    "last_used_at = EXCLUDED.last_used_at, uses = EXCLUDED.uses, "
                    "expires_at = EXCLUDED.expires_at"
                ),
                {
                    "runtime_id": self._scope.runtime_id,
                    "subject_key": self._scope.subject_key,
                    "id": fact.id,
                    "content": fact.content,
                    "created_at": fact.created_at,
                    "last_used_at": fact.last_used_at,
                    "uses": fact.uses,
                    "expires_at": fact.expires_at,
                },
            )

    async def remove(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        async with self._transaction() as conn:
            result = await conn.execute(
                text(f"DELETE FROM {USER_FACTS_TABLE} WHERE id = ANY(:ids)"),
                {"ids": list(ids)},
            )
            return int(result.rowcount)

    async def touch(self, ids: Sequence[str], now: datetime) -> None:
        if not ids:
            return
        async with self._transaction() as conn:
            await conn.execute(
                text(
                    f"UPDATE {USER_FACTS_TABLE} SET uses = uses + 1, "
                    "last_used_at = :now WHERE id = ANY(:ids)"
                ),
                {"ids": list(ids), "now": now},
            )


class PostgresUserFactStore:
    name = USER_FACTS_STORE
    tables = frozenset({USER_FACTS_TABLE})

    def __init__(self, provider: PostgresScopeProvider | None = None) -> None:
        self._provider = provider

    @property
    def unbound(self) -> bool:
        """True for the import-time placeholder that holds no provider."""
        return self._provider is None

    def _backend(self, subject: SubjectRef) -> PostgresFactBackend:
        assert self._provider is not None
        return PostgresFactBackend(
            self._provider.scope(subject.runtime_id, subject.subject_key)
        )

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        if self._provider is None:
            return []
        now = datetime.now(UTC)
        return [
            export_row(f)
            for f in await self._backend(subject).all()
            if not f.expired(now)
        ]

    async def purge(self, *, retention_s: int) -> int:
        """Delete every expired fact of every subject (``retention_s`` is unused).

        A fact's lifetime is its own ``expires_at``, set from the configured
        retention when it was written.
        """
        if self._provider is None:
            return 0
        async with self._provider.maintenance(PURGE_EXPIRED) as conn:
            result = await conn.execute(
                text(f"DELETE FROM {USER_FACTS_TABLE} WHERE expires_at <= now()")
            )
            return int(result.rowcount)

    async def delete(self, subject: SubjectRef) -> int:
        if self._provider is None:
            return 0
        async with self._provider.scope(
            subject.runtime_id, subject.subject_key
        ).transaction() as conn:
            result = await conn.execute(text(f"DELETE FROM {USER_FACTS_TABLE}"))
            return int(result.rowcount)


#: The unbound placeholder, registered so the migration's table has a store.
USER_FACTS_STORE_PLACEHOLDER = PostgresUserFactStore()
if USER_FACTS_STORE not in DEFAULT_REGISTRY.stores:
    DEFAULT_REGISTRY.register(USER_FACTS_STORE_PLACEHOLDER)
