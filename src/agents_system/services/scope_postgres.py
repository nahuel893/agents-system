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

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
except ImportError as exc:
    raise_missing_extra("postgres", "The Postgres scope backend", exc)

RUNTIME_SETTING = "app.runtime_id"
SUBJECT_SETTING = "app.subject_key"
POLICY_NAME = "subject_scope"
RESTRICTIVE_POLICY_NAME = "subject_scope_restrictive"

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$")


class UnfencedRoleError(RuntimeError):
    """The database role bypasses row-level security, so nothing is fenced."""


def subject_policy_statements(table: str) -> list[str]:
    """DDL that fences ``table`` (columns ``runtime_id``, ``subject_key``)."""
    if not _IDENTIFIER.match(table):
        raise ValueError(f"not a plain table identifier: {table!r}")
    match = (
        f"runtime_id = nullif(current_setting('{RUNTIME_SETTING}', true), '') "
        f"AND subject_key = nullif(current_setting('{SUBJECT_SETTING}', true), '')"
    )
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


async def create_subject_policy(conn: AsyncConnection, table: str) -> None:
    """Create the row-level security policy for a per-user table."""
    for statement in subject_policy_statements(table):
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

    def scope(self, runtime_id: str, subject_key: str) -> PostgresScope:
        return PostgresScope(self._engine, runtime_id, subject_key)

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
