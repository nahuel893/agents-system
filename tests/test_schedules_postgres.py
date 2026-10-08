"""Schedules against Postgres (#175): fenced per user, claimed once.

Needs ``DATABASE_URL`` (an admin URL, used only to provision a non-superuser
role and a throwaway table) and skips without it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.schedules import Recurrence, Schedule, schedule_backend_for
from agents_system.services.schedules_postgres import (
    PostgresScheduleBackend,
    PostgresScheduleStore,
    schedule_table_statements,
)
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject_data import SubjectRef

pytestmark = pytest.mark.integration

RT = "rt"
ALICE, BOB = "participant:1", "participant:2"
NOW = datetime(2026, 10, 5, 7, 1, tzinfo=UTC)


@pytest.fixture
async def env() -> AsyncIterator[tuple[PostgresScopeProvider, str]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    suffix = uuid.uuid4().hex[:8]
    role, table, pw = f"sc_app_{suffix}", f"sc_sched_{suffix}", "pw" + suffix
    admin_url = make_url(url)
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        for statement in schedule_table_statements(table):
            await conn.execute(text(statement))
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        )
    app: AsyncEngine = create_async_engine(admin_url.set(username=role, password=pw))
    try:
        yield PostgresScopeProvider(app), table
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


def _schedule(subject: str, sid: str, due: datetime = NOW) -> Schedule:
    return Schedule(
        id=sid,
        runtime_id=RT,
        subject_key=subject,
        prompt="weekly sales",
        recurrence=Recurrence("weekly", time_of_day=time(8, 0), weekday=0, tz="UTC"),
        next_run_at=due,
        created_at=NOW - timedelta(days=1),
    )


async def test_backend_round_trip_and_isolation(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, table = env
    alice = PostgresScheduleBackend(provider.scope(RT, ALICE), table=table)
    bob = PostgresScheduleBackend(provider.scope(RT, BOB), table=table)
    assert await alice.add(_schedule(ALICE, "a1"), max_per_user=3)
    listed = await alice.list()
    assert [s.id for s in listed] == ["a1"]
    assert listed[0].recurrence == Recurrence(
        "weekly", time_of_day=time(8, 0), weekday=0, tz="UTC"
    )
    assert await bob.list() == []
    assert await bob.remove("a1") is False
    assert await alice.remove("a1") is True


async def test_the_cap_holds_under_concurrent_creates(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, table = env
    backend = PostgresScheduleBackend(provider.scope(RT, ALICE), table=table)
    results = await asyncio.gather(
        *[backend.add(_schedule(ALICE, f"s{i}"), max_per_user=2) for i in range(6)]
    )
    assert sum(results) == 2
    assert len(await backend.list()) == 2


async def test_a_due_schedule_is_claimed_once_across_workers(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, table = env
    store = PostgresScheduleStore(provider, table=table)
    await PostgresScheduleBackend(provider.scope(RT, ALICE), table=table).add(
        _schedule(ALICE, "a1"), max_per_user=3
    )
    claims = await asyncio.gather(
        *[
            store.claim_due(NOW, lambda s, at: s.recurrence.next_after(at))
            for _ in range(4)
        ]
    )
    won = [c for c in claims if c is not None]
    assert len(won) == 1
    assert won[0].due_at == NOW
    assert won[0].schedule.next_run_at == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    stored = (
        await PostgresScheduleBackend(provider.scope(RT, ALICE), table=table).list()
    )[0]
    assert stored.next_run_at == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    await store.record(RT, ALICE, "a1", "delivered", NOW)
    stored = (
        await PostgresScheduleBackend(provider.scope(RT, ALICE), table=table).list()
    )[0]
    assert stored.last_status == "delivered"
    assert await store.claim_due(NOW, lambda s, at: s.recurrence.next_after(at)) is None


async def test_the_fence_hides_other_subjects_without_the_worker_setting(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, table = env
    await PostgresScheduleBackend(provider.scope(RT, ALICE), table=table).add(
        _schedule(ALICE, "a1"), max_per_user=3
    )
    async with provider.engine.begin() as conn:
        count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    assert count == 0


async def test_export_and_delete_reach_only_the_subject(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, table = env
    store = PostgresScheduleStore(provider, table=table)
    for subject, sid in ((ALICE, "a1"), (BOB, "b1")):
        await PostgresScheduleBackend(provider.scope(RT, subject), table=table).add(
            _schedule(subject, sid), max_per_user=3
        )
    rows = await store.export(SubjectRef(RT, ALICE))
    assert [r["id"] for r in rows] == ["a1"]
    assert await store.delete(SubjectRef(RT, ALICE)) == 1
    assert await store.export(SubjectRef(RT, ALICE)) == []
    assert len(await store.export(SubjectRef(RT, BOB))) == 1


def test_the_handle_picks_the_postgres_backend(
    env: tuple[PostgresScopeProvider, str],
) -> None:
    provider, _ = env
    assert isinstance(
        schedule_backend_for(provider.scope(RT, ALICE)), PostgresScheduleBackend
    )
