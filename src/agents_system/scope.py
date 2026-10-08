"""The scope handle a subject-scoped tool receives (ADR-011 part 3).

A tool that touches per-user data never holds a subject key. The interceptor
gives its connector a ``scope=`` handle bound to (runtime id, subject key);
the handle's methods take neither. The backend behind the handle fences the
rows, so a query that forgets its filter still sees only the current
subject's: Postgres does it with row-level security
(`agents_system.services.scope_postgres`), and `InMemoryScopeProvider` here
keeps the same contract over a dict keyed by the pair, for library mode.

This module has no dependency on the rest of the package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SubjectScope(Protocol):
    """A handle bound to one (runtime id, subject key) pair."""

    @property
    def runtime_id(self) -> str: ...

    @property
    def subject_key(self) -> str: ...


@runtime_checkable
class ScopeProvider(Protocol):
    """Builds a handle for a (runtime id, subject key) pair."""

    def scope(self, runtime_id: str, subject_key: str) -> SubjectScope: ...


class InMemoryScope:
    """Reference handle: rows live in a dict keyed by the pair."""

    def __init__(
        self,
        runtime_id: str,
        subject_key: str,
        store: dict[tuple[str, str], dict[str, dict[str, Any]]],
        locks: dict[tuple[str, str], asyncio.Lock] | None = None,
    ) -> None:
        self._runtime_id = runtime_id
        self._subject_key = subject_key
        self._store = store
        self._locks = locks if locks is not None else {}

    @property
    def lock(self) -> asyncio.Lock:
        """One lock per (runtime id, subject key), shared by every handle of it.

        A read-modify-write over this scope (remember then evict) holds it so
        a concurrent writer of the same subject cannot interleave.
        """
        key = (self._runtime_id, self._subject_key)
        return self._locks.setdefault(key, asyncio.Lock())

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def subject_key(self) -> str:
        return self._subject_key

    def _tables(self) -> dict[str, dict[str, Any]]:
        return self._store.setdefault((self._runtime_id, self._subject_key), {})

    def put(self, table: str, key: str, value: Any) -> None:
        self._tables().setdefault(table, {})[key] = value

    def get(self, table: str, key: str) -> Any | None:
        return self._tables().get(table, {}).get(key)

    def delete(self, table: str, key: str) -> None:
        self._tables().get(table, {}).pop(key, None)

    def rows(self, table: str) -> dict[str, Any]:
        """Every row of ``table`` in this scope: the deliberately unfiltered read."""
        return dict(self._tables().get(table, {}))


class InMemoryScopeProvider:
    """In-process reference backend (ADR-002 C.15): no Postgres needed."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def scope(self, runtime_id: str, subject_key: str) -> InMemoryScope:
        return InMemoryScope(runtime_id, subject_key, self._store, self._locks)

    def sweep(self, table: str, expired: Callable[[Any], bool]) -> int:
        """Delete, across every subject, the rows of ``table`` that ``expired``.

        The maintenance path of the reference backend, for a retention purge
        that has no single subject's scope to run in.
        """
        removed = 0
        for tables in self._store.values():
            rows = tables.get(table, {})
            for key in [k for k, v in rows.items() if expired(v)]:
                del rows[key]
                removed += 1
        return removed


async def assert_subject_isolation(
    provider: ScopeProvider,
    *,
    write: Callable[[Any, str], Awaitable[None]],
    read_all: Callable[[Any], Awaitable[list[str]]],
    runtime_id: str = "isolation-test",
) -> None:
    """Structural test every per-user store ships (ADR-011 part 3).

    Two subjects each write a distinct value through their own handle, then
    each runs ``read_all``, which must be a deliberately UNFILTERED query
    (no subject predicate). Each must get back only its own value, and a
    third subject nothing. Raises ``AssertionError`` when the backend leaks.
    """
    handles = {
        name: provider.scope(runtime_id, f"participant:{name}")
        for name in ("alice", "bob", "carol")
    }
    await write(handles["alice"], "alice-value")
    await write(handles["bob"], "bob-value")

    assert await read_all(handles["alice"]) == ["alice-value"], (
        "an unfiltered read in alice's scope must return only alice's rows"
    )
    assert await read_all(handles["bob"]) == ["bob-value"], (
        "an unfiltered read in bob's scope must return only bob's rows"
    )
    assert await read_all(handles["carol"]) == [], (
        "a subject that wrote nothing must see no rows"
    )
