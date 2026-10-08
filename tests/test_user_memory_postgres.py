"""Per-user facts on Postgres: the real migration, row-level security (#250).

Needs ``DATABASE_URL`` (an admin URL, used only to provision a throwaway
schema and a non-superuser role) and skips without it.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.scope import assert_subject_isolation
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.services.user_facts_postgres import PostgresUserFactStore
from agents_system.subject_data import SubjectRef
from agents_system.user_memory import UserFacts, UserMemoryLimits, backend_for

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "010_subject_facts.py"
)
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_010", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(context: MigrationContext, migration: object) -> None:
    with Operations.context(context):
        migration.upgrade()  # type: ignore[attr-defined]


@pytest.fixture
async def provider() -> AsyncIterator[PostgresScopeProvider]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    suffix = uuid.uuid4().hex[:8]
    role, schema, pw = f"uf_app_{suffix}", f"uf_{suffix}", "pw" + suffix
    admin_url = make_url(url)
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
        await conn.execute(text(f"SET search_path TO {schema}"))
        migration = _migration()
        await conn.run_sync(
            lambda sync: _run_upgrade(MigrationContext.configure(sync), migration)
        )
        await conn.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.subject_facts "
                f"TO {role}"
            )
        )
    app = create_async_engine(
        admin_url.set(username=role, password=pw),
        connect_args={"server_settings": {"search_path": schema}},
    )
    prov = PostgresScopeProvider(app)
    await prov.verify()
    try:
        yield prov
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


def _facts(
    provider: PostgresScopeProvider,
    subject: str = "participant:a",
    limits: UserMemoryLimits | None = None,
    runtime_id: str = "rt",
) -> UserFacts:
    return UserFacts(
        backend_for(provider.scope(runtime_id, subject)), limits or UserMemoryLimits()
    )


async def test_an_unfiltered_read_sees_only_the_scoped_subject(
    provider: PostgresScopeProvider,
) -> None:
    async def write(scope: Any, value: str) -> None:
        await UserFacts(backend_for(scope), UserMemoryLimits()).remember(value)

    async def read_all(scope: Any) -> list[str]:
        facts = UserFacts(backend_for(scope), UserMemoryLimits())
        return [f.content for f in await facts.list()]

    await assert_subject_isolation(provider, write=write, read_all=read_all)


async def test_a_raw_query_without_a_predicate_is_still_fenced(
    provider: PostgresScopeProvider,
) -> None:
    await _facts(provider, "participant:a").remember("a's fact")
    await _facts(provider, "participant:b").remember("b's fact")

    async with provider.scope("rt", "participant:a").transaction() as conn:
        rows = (await conn.execute(text("SELECT content FROM subject_facts"))).all()
    assert [r[0] for r in rows] == ["a's fact"]

    # A transaction with no scope sees nothing at all.
    async with provider._engine.begin() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM subject_facts"))
        ).scalar_one() == 0


async def test_facts_survive_and_dedupe_across_calls(
    provider: PostgresScopeProvider,
) -> None:
    await _facts(provider).remember("Prefers metric units", now=NOW)
    await _facts(provider).remember("prefers  metric units", now=NOW)

    [fact] = await _facts(provider).list(now=NOW)
    assert fact.content == "prefers metric units"


async def test_eviction_ttl_and_use_counts(provider: PostgresScopeProvider) -> None:
    facts = _facts(provider, limits=UserMemoryLimits(max_facts=2, ttl_s=100))
    a = await facts.remember("a", now=NOW)
    await facts.remember("b", now=NOW + timedelta(seconds=1))
    await facts.touch([a.id], now=NOW + timedelta(seconds=2))
    await facts.remember("c", now=NOW + timedelta(seconds=3))

    kept = {f.content for f in await facts.list(now=NOW + timedelta(seconds=4))}
    assert kept == {"a", "c"}
    assert await facts.list(now=NOW + timedelta(seconds=500)) == []
    # The expired rows were dropped by that read, not just hidden.
    async with provider.scope("rt", "participant:a").transaction() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM subject_facts"))
        ).scalar_one()
    assert count == 0


async def test_export_and_delete_reach_one_subject_only(
    provider: PostgresScopeProvider,
) -> None:
    await _facts(provider, "participant:a").remember("a1")
    await _facts(provider, "participant:b").remember("b1")
    store = PostgresUserFactStore(provider)

    assert [
        r["content"] for r in await store.export(SubjectRef("rt", "participant:a"))
    ] == ["a1"]
    assert await store.delete(SubjectRef("rt", "participant:a")) == 1
    assert await store.export(SubjectRef("rt", "participant:a")) == []
    assert [
        r["content"] for r in await store.export(SubjectRef("rt", "participant:b"))
    ] == ["b1"]


async def test_the_unbound_placeholder_holds_nothing() -> None:
    store = PostgresUserFactStore()
    assert store.unbound
    assert await store.export(SubjectRef("rt", "participant:a")) == []
    assert await store.delete(SubjectRef("rt", "participant:a")) == 0
