"""Proactive queue against Postgres (#251): once across workers and a restart.

Needs ``DATABASE_URL`` (an admin URL, used only to provision a non-superuser
role and a throwaway table) and skips without it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.proactive import (
    ProactiveDispatcher,
    ProactiveSender,
    StaticRecipientDirectory,
)
from agents_system.scope import assert_subject_isolation
from agents_system.services.proactive_postgres import (
    PostgresProactiveQueue,
    proactive_table_statements,
)
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject import TurnActor, TurnSubject
from agents_system.subject_data import SubjectDataRegistry, SubjectRef, export_subject

pytestmark = pytest.mark.integration

RT = "rt"
ALICE, BOB = TurnSubject.for_participant(1), TurnSubject.for_participant(2)
NOW = datetime.now(UTC)
GRANTS = {RT: {"send:proactive"}}


class Channel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_proactive(self, to: str, body: str) -> None:
        self.calls.append((to, body))
        await asyncio.sleep(0.01)


@pytest.fixture
async def env() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine, str]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    suffix = uuid.uuid4().hex[:8]
    role, table, pw = f"pq_app_{suffix}", f"pq_outbox_{suffix}", "pw" + suffix
    admin_url = make_url(url)
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS")
        )
        for statement in proactive_table_statements(table):
            await conn.execute(text(statement))
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        )
    app = create_async_engine(admin_url.set(username=role, password=pw))
    try:
        yield admin, app, table
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


def _stack(
    app: AsyncEngine, table: str, channel: Channel, *, now: datetime = NOW
) -> tuple[ProactiveSender, ProactiveDispatcher]:
    """A fresh queue, directory and dispatcher: what a restarted worker builds."""
    queue = PostgresProactiveQueue(PostgresScopeProvider(app), table=table)
    directory = StaticRecipientDirectory(
        {
            (RT, s.key): TurnSubject(s.key, channel="whatsapp", address=f"54{i}")
            for i, s in enumerate((ALICE, BOB))
        }
    )
    sender = ProactiveSender(queue, directory, deploy_grants=GRANTS, clock=lambda: now)
    dispatcher = ProactiveDispatcher(
        queue,
        directory,
        {"whatsapp": channel},
        deploy_grants=GRANTS,
        clock=lambda: now,
    )
    return sender, dispatcher


async def test_a_message_is_delivered_once_across_two_workers_and_a_restart(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = env
    channel = Channel()
    sender, worker_a = _stack(app, table, channel)
    await sender.submit(
        RT,
        ALICE,
        "hello",
        actor=TurnActor("scheduler", "s"),
        permissions={"send:proactive"},
    )
    _, worker_b = _stack(app, table, channel)
    results = await asyncio.gather(worker_a.run_once("a"), worker_b.run_once("b"))
    assert sorted(r.status for r in results) == ["idle", "sent"]
    # A restart: brand new queue, directory and dispatcher over the same table.
    _, restarted = _stack(app, table, channel, now=NOW + timedelta(hours=1))
    assert (await restarted.run_once("c")).status == "idle"
    assert channel.calls == [("540", "hello")]


async def test_a_crash_after_claiming_is_never_resent(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = env
    channel = Channel()
    sender, _ = _stack(app, table, channel)
    queue = PostgresProactiveQueue(PostgresScopeProvider(app), table=table)
    await sender.submit(
        RT,
        ALICE,
        "hello",
        actor=TurnActor("operator", "o"),
        permissions={"send:proactive"},
    )
    assert await queue.claim("dead", now=NOW, lease_s=30) is not None
    _, restarted = _stack(app, table, channel, now=NOW + timedelta(minutes=5))
    assert (await restarted.run_once("w")).status == "idle"
    assert channel.calls == []
    exported = await queue.export(SubjectRef(RT, ALICE.key))
    assert [r["status"] for r in exported] == ["ambiguous"]


async def test_the_table_is_fenced_per_subject(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = env
    provider = PostgresScopeProvider(app)

    async def write(scope: object, value: str) -> None:
        async with scope.transaction() as conn:  # type: ignore[attr-defined]
            await conn.execute(
                text(
                    f"INSERT INTO {table} (id, runtime_id, subject_key, body, actor,"
                    " available_at) VALUES (:id, :r, :s, :b, 'x', now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "r": scope.runtime_id,
                    "s": scope.subject_key,
                    "b": value,
                },  # type: ignore[attr-defined]
            )

    async def read_all(scope: object) -> list[str]:
        async with scope.transaction() as conn:  # type: ignore[attr-defined]
            return [r[0] for r in await conn.execute(text(f"SELECT body FROM {table}"))]

    await assert_subject_isolation(provider, write=write, read_all=read_all)


async def test_export_delete_and_purge(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    admin, app, table = env
    channel = Channel()
    sender, dispatcher = _stack(app, table, channel)
    queue = PostgresProactiveQueue(PostgresScopeProvider(app), table=table)
    for subject, body in ((ALICE, "a"), (BOB, "b")):
        await sender.submit(
            RT,
            subject,
            body,
            actor=TurnActor("library", "t"),
            permissions={"send:proactive"},
        )
    registry = SubjectDataRegistry()
    registry.register(queue)
    exported = await export_subject(registry, SubjectRef(RT, ALICE.key))
    assert [r["body"] for r in exported["stores"]["proactive"]] == ["a"]
    assert await queue.delete(SubjectRef(RT, ALICE.key)) == 1
    assert await queue.export(SubjectRef(RT, ALICE.key)) == []
    assert (await dispatcher.run_once("w")).status == "sent"
    async with admin.begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET finished_at = now() - interval '2 days'")
        )
    assert await queue.purge(retention_s=3600) == 1


async def test_expire_leases_returns_each_lapsed_message_once(
    env: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = env
    sender, _ = _stack(app, table, Channel())
    queue = PostgresProactiveQueue(PostgresScopeProvider(app), table=table)
    await sender.submit(
        RT,
        ALICE,
        "hello",
        actor=TurnActor("operator", "o"),
        permissions={"send:proactive"},
    )
    assert await queue.claim("dead", now=NOW, lease_s=30) is not None
    later = NOW + timedelta(minutes=5)
    expired = await queue.expire_leases(later)
    assert [(m.runtime_id, m.subject_key, m.actor, m.status) for m in expired] == [
        (RT, ALICE.key, "operator:o", "ambiguous")
    ]
    assert await queue.expire_leases(later) == []
