"""Registry against Postgres: a fenced store, and the unregistered-table guard (#257).

Needs ``DATABASE_URL`` (an admin URL, used only to provision a non-superuser
role and throwaway tables) and skips without it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.services.scope_postgres import (
    PostgresScopeProvider,
    create_subject_policy,
    find_unregistered_subject_tables,
)
from agents_system.subject import TurnActor
from agents_system.subject_data import (
    SubjectDataRegistry,
    SubjectRef,
    delete_subject,
    export_subject,
)

pytestmark = pytest.mark.integration


class NotesStore:
    """A per-user store over a fenced table, reading and deleting through the scope."""

    name = "notes"

    def __init__(self, provider: PostgresScopeProvider, table: str) -> None:
        self._provider, self._table = provider, table
        self.tables = frozenset({table})

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        async with scope.transaction() as conn:
            rows = await conn.execute(text(f"SELECT body FROM {self._table}"))
            return [{"body": r[0]} for r in rows]

    async def delete(self, subject: SubjectRef) -> int:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        async with scope.transaction() as conn:
            return (await conn.execute(text(f"DELETE FROM {self._table}"))).rowcount


@pytest.fixture
async def env() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine, str]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    suffix = uuid.uuid4().hex[:8]
    role, table, pw = f"sd_app_{suffix}", f"sd_notes_{suffix}", "pw" + suffix
    admin_url = make_url(url)
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await conn.execute(
            text(
                f"CREATE TABLE {table} (runtime_id text NOT NULL,"
                " subject_key text NOT NULL, body text NOT NULL)"
            )
        )
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        )
        await create_subject_policy(conn, table)
    app = create_async_engine(admin_url.set(username=role, password=pw))
    try:
        yield admin, app, table
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


async def test_export_and_delete_touch_only_the_named_subject(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = env
    provider = PostgresScopeProvider(app)
    registry = SubjectDataRegistry()
    registry.register(NotesStore(provider, table))
    alice, bob = SubjectRef("rt", "participant:1"), SubjectRef("rt", "participant:2")
    for ref, body in ((alice, "a"), (bob, "b")):
        async with provider.scope(ref.runtime_id, ref.subject_key).transaction() as c:
            await c.execute(
                text(f"INSERT INTO {table} VALUES (:r, :s, :b)"),
                {"r": ref.runtime_id, "s": ref.subject_key, "b": body},
            )

    exported = await export_subject(registry, alice)
    assert exported["stores"] == {"notes": [{"body": "a"}]}
    result = await delete_subject(
        registry, alice, actor=TurnActor(kind="operator", id="ops")
    )
    assert result["stores"] == {"notes": 1}
    assert (await export_subject(registry, alice))["stores"] == {"notes": []}
    assert (await export_subject(registry, bob))["stores"] == {"notes": [{"body": "b"}]}


async def test_a_table_with_a_subject_key_and_no_store_is_reported(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    admin, app, table = env
    empty = SubjectDataRegistry()
    assert table in await find_unregistered_subject_tables(admin, empty)
    registered = SubjectDataRegistry()
    registered.register(NotesStore(PostgresScopeProvider(app), table))
    assert table not in await find_unregistered_subject_tables(admin, registered)
