"""Postgres backend of the scope handle: row-level security (ADR-011 part 3).

The handle opens each transaction with the runtime id and subject key as
transaction-local settings (``app.runtime_id``, ``app.subject_key``); a policy
on each per-user table compares its ``runtime_id`` and ``subject_key`` columns
to them, with ``FORCE ROW LEVEL SECURITY`` so the table owner is fenced too.
The predicate is installed as a permissive and as a restrictive policy: the
restrictive one is AND-combined with any policy already on the table, so a
legacy permissive policy cannot widen it. A query that forgets its filter therefore still returns only the current
subject's rows, and a transaction with no scope sees none.

Postgres superusers and roles with ``BYPASSRLS`` skip row-level security
entirely, even under FORCE. ``PostgresScopeProvider.verify`` refuses such a
role, and the served app calls it at boot when a per-user feature is on.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents_system.extras import raise_missing_extra
from agents_system.subject_data import SubjectDataRegistry

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres scope backend", exc)

RUNTIME_SETTING = "app.runtime_id"
SUBJECT_SETTING = "app.subject_key"
MAINTENANCE_SETTING = "app.maintenance"
PURGE_EXPIRED = "purge_expired"
POLICY_NAME = "subject_scope"
RESTRICTIVE_POLICY_NAME = "subject_scope_restrictive"

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


class UnfencedRoleError(RuntimeError):
    """The database role bypasses row-level security, so nothing is fenced."""


def subject_policy_statements(
    table: str, *, open_setting: str | None = None
) -> list[str]:
    """DDL that fences ``table`` (columns ``runtime_id``, ``subject_key``).

    ``open_setting`` names a transaction-local setting that, set to ``on``,
    lifts the fence for a platform worker that must act across subjects (the
    proactive dispatcher claims whichever message is due). Tool code never
    sets it: a scope handle only sets the runtime and subject settings.
    """
    if not _IDENTIFIER.match(table):
        raise ValueError(f"not a plain table identifier: {table!r}")
    match = (
        f"runtime_id = nullif(current_setting('{RUNTIME_SETTING}', true), '') "
        f"AND subject_key = nullif(current_setting('{SUBJECT_SETTING}', true), '')"
    )
    if open_setting is not None:
        if not re.fullmatch(r"[a-z_]+\.[a-z_]+", open_setting):
            raise ValueError(f"not a plain setting name: {open_setting!r}")
        match = f"(({match}) OR current_setting('{open_setting}', true) = 'on')"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}",
        f"DROP POLICY IF EXISTS {RESTRICTIVE_POLICY_NAME} ON {table}",
        f"CREATE POLICY {POLICY_NAME} ON {table} USING ({match}) WITH CHECK ({match})",
        # Permissive policies are OR-combined, so a pre-existing permissive
        # policy would widen ours. The restrictive twin is AND-combined with
        # every permissive policy, which keeps the subject predicate binding.
        (
            f"CREATE POLICY {RESTRICTIVE_POLICY_NAME} ON {table} AS RESTRICTIVE "
            f"USING ({match}) WITH CHECK ({match})"
        ),
    ]


def expired_purge_policy_statements(table: str) -> list[str]:
    """DDL that lets a retention purge reach the expired rows of every subject.

    ``table`` already has the subject policy and an ``expires_at`` column. A
    purge has no single subject's scope to run in, so it opens a transaction
    with ``app.maintenance = 'purge_expired'`` (`PostgresScopeProvider.maintenance`).
    Under that setting only, rows past ``expires_at`` become visible and
    deletable: never writable, never a live row, and a transaction without the
    setting is as fenced as before.
    """
    if not _IDENTIFIER.match(table):
        raise ValueError(f"not a plain table identifier: {table!r}")
    match = (
        f"runtime_id = nullif(current_setting('{RUNTIME_SETTING}', true), '') "
        f"AND subject_key = nullif(current_setting('{SUBJECT_SETTING}', true), '')"
    )
    purge = (
        f"current_setting('{MAINTENANCE_SETTING}', true) = '{PURGE_EXPIRED}' "
        "AND expires_at IS NOT NULL AND expires_at <= now()"
    )
    return [
        f"DROP POLICY IF EXISTS {RESTRICTIVE_POLICY_NAME} ON {table}",
        (
            f"CREATE POLICY {RESTRICTIVE_POLICY_NAME} ON {table} AS RESTRICTIVE "
            f"USING ({match} OR ({purge})) WITH CHECK ({match})"
        ),
        f"DROP POLICY IF EXISTS {POLICY_NAME}_purge_select ON {table}",
        f"DROP POLICY IF EXISTS {POLICY_NAME}_purge_delete ON {table}",
        f"CREATE POLICY {POLICY_NAME}_purge_select ON {table} FOR SELECT USING ({purge})",
        f"CREATE POLICY {POLICY_NAME}_purge_delete ON {table} FOR DELETE USING ({purge})",
    ]


async def create_subject_policy(
    conn: AsyncConnection, table: str, *, open_setting: str | None = None
) -> None:
    """Create the row-level security policy for a per-user table."""
    for statement in subject_policy_statements(table, open_setting=open_setting):
        await conn.execute(text(statement))


class PostgresScope:
    """A handle bound to (runtime id, subject key) over one engine."""

    def __init__(self, engine: AsyncEngine, runtime_id: str, subject_key: str) -> None:
        self._engine = engine
        self._runtime_id = runtime_id
        self._subject_key = subject_key

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def subject_key(self) -> str:
        return self._subject_key

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        """One transaction with the scope settings applied (transaction-local)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config(:rk, :rv, true), set_config(:sk, :sv, true)"),
                {
                    "rk": RUNTIME_SETTING,
                    "rv": self._runtime_id,
                    "sk": SUBJECT_SETTING,
                    "sv": self._subject_key,
                },
            )
            yield conn


class PostgresScopeProvider:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def scope(self, runtime_id: str, subject_key: str) -> PostgresScope:
        return PostgresScope(self._engine, runtime_id, subject_key)

    @asynccontextmanager
    async def maintenance(self, purpose: str) -> AsyncIterator[AsyncConnection]:
        """One transaction under a maintenance purpose, with no subject scope.

        Only a table that carries a policy for *purpose* (see
        `expired_purge_policy_statements`) opens anything to it, and then only
        the rows that policy names.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config(:k, :v, true)"),
                {"k": MAINTENANCE_SETTING, "v": purpose},
            )
            yield conn

    async def verify(self) -> None:
        """Refuse a superuser or BYPASSRLS role: it would see every row."""
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        if row.rolsuper or row.rolbypassrls:
            raise UnfencedRoleError(
                f"database role {row.current_user!r} is a superuser or holds "
                "BYPASSRLS, so row-level security does not apply to it; connect "
                "per-user features as a dedicated non-superuser role"
            )


async def find_unregistered_subject_tables(
    engine: AsyncEngine, registry: SubjectDataRegistry
) -> set[str]:
    """Tables with a ``subject_key`` column that no registered store claims.

    The guard behind ADR-011 part 6: a per-user table must have a store that
    can export and delete it, so a test asserts this is empty and a new store
    cannot be forgotten.
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'subject_key' AND table_schema = current_schema()"
            )
        )
        found = {r[0] for r in rows}
    return found - set(registry.tables)
