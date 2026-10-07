"""Integration proof for the durable Postgres checkpointer (issue #24).

Set CHECKPOINTER_TEST_DATABASE_URL to an isolated, disposable PostgreSQL
database (``postgresql+asyncpg://...``). These tests run Alembic upgrades and
downgrades and delete every checkpoint, so they must never target a database
with user data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import text

from agents_system.agent.graph import AgentRuntime
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.models.base import get_engine
from agents_system.services.checkpointer import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointSchemaError,
    CheckpointThreads,
    open_postgres_checkpointer,
    purge_checkpoints,
)

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _url() -> str:
    url = os.environ.get("CHECKPOINTER_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "CHECKPOINTER_TEST_DATABASE_URL is required and must name an "
            "isolated throwaway PostgreSQL database; this test runs Alembic "
            "downgrade."
        )
    assert url is not None
    return url


def _alembic(command: str, url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {command} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


async def _scalar(url: str, sql: str, **params: Any) -> Any:
    engine = get_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(text(sql), params)
    finally:
        await engine.dispose()


async def _execute(url: str, sql: str, **params: Any) -> None:
    engine = get_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrated() -> AsyncIterator[str]:
    url = _url()
    _alembic("upgrade head", url)
    try:
        yield url
    finally:
        _alembic("downgrade base", url)


class _Model(FakeMessagesListChatModel):
    """Answers in order and remembers what the last call received."""

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        object.__setattr__(self, "seen", list(input))
        return await super().ainvoke(input, *args, **kwargs)


def _runtime() -> EquippedRuntime:
    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    return EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )


async def _turns(url: str, thread_id: str, texts: Sequence[str]) -> list[str]:
    """Run one turn per text on *thread_id*; return what the LAST model call
    saw (human and AI contents, system prompt excluded)."""
    model = _Model(responses=[AIMessage(content=f"re: {t}") for t in texts])
    async with open_postgres_checkpointer(url, pool_size=2, timeout_s=5.0) as saver:
        agent = AgentRuntime(_runtime(), model, checkpointer=saver)
        for t in texts:
            await agent.run_turn(
                [HumanMessage(content=t)],
                session_id="s",
                permissions=(),
                thread_id=thread_id,
            )
    return [m.content for m in model.seen[1:]]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Schema: Alembic owns when, the saver owns what
# ---------------------------------------------------------------------------


async def test_alembic_creates_the_tables_at_the_savers_schema_version() -> None:
    url = _url()
    _alembic("upgrade head", url)
    try:
        version = await _scalar(url, "SELECT max(v) FROM checkpoint_migrations")
        assert version == CHECKPOINT_SCHEMA_VERSION
        for table in _TABLES:
            assert await _scalar(url, "SELECT to_regclass(:t)::text", t=table) == table
        _alembic("downgrade 006", url)
        for table in (*_TABLES, "checkpoint_migrations"):
            assert await _scalar(url, "SELECT to_regclass(:t)::text", t=table) is None
    finally:
        _alembic("downgrade base", url)


async def test_the_checkpointer_refuses_an_unmigrated_database() -> None:
    url = _url()
    _alembic("upgrade 006", url)
    try:
        with pytest.raises(CheckpointSchemaError, match="alembic upgrade head"):
            async with open_postgres_checkpointer(url, pool_size=1, timeout_s=5.0):
                pass
    finally:
        _alembic("downgrade base", url)


_APP_ROLE = "checkpointer_it_app_role"


@pytest.mark.parametrize(
    ("grants", "read_only", "refused_with"),
    [
        ("SELECT", False, "INSERT on checkpoint_blobs"),
        ("SELECT, INSERT, UPDATE", False, "DELETE on checkpoints"),
        ("SELECT, INSERT, UPDATE, DELETE", True, "read-only"),
        ("SELECT, INSERT, UPDATE, DELETE", False, None),
    ],
)
async def test_the_checkpointer_refuses_a_role_that_cannot_save_or_purge(
    migrated: str, grants: str, read_only: bool, refused_with: str | None
) -> None:
    """A separate app role is an expected setup: the migration role creates
    the tables. One that can read them but not write or delete would boot
    and then forget every conversation and never purge one."""
    from sqlalchemy.engine import make_url

    await _execute(
        migrated, f"CREATE ROLE {_APP_ROLE} LOGIN PASSWORD 'app-role-password'"
    )
    try:
        await _execute(
            migrated,
            f"GRANT {grants} ON checkpoints, checkpoint_blobs, checkpoint_writes, "
            f"checkpoint_migrations TO {_APP_ROLE}",
        )
        if read_only:
            await _execute(
                migrated,
                f"ALTER ROLE {_APP_ROLE} SET default_transaction_read_only = on",
            )
        app_url = (
            make_url(migrated)
            .set(username=_APP_ROLE, password="app-role-password")
            .render_as_string(hide_password=False)
        )

        if refused_with is None:
            async with open_postgres_checkpointer(app_url, pool_size=1, timeout_s=5.0):
                pass
        else:
            with pytest.raises(CheckpointSchemaError, match=refused_with):
                async with open_postgres_checkpointer(
                    app_url, pool_size=1, timeout_s=5.0
                ):
                    pass
    finally:
        await _execute(migrated, f"DROP OWNED BY {_APP_ROLE}")
        await _execute(migrated, f"DROP ROLE {_APP_ROLE}")


# ---------------------------------------------------------------------------
# Durability: a conversation survives a process restart
# ---------------------------------------------------------------------------

_ONE_TURN_IN_A_FRESH_PROCESS = textwrap.dedent(
    """
    import asyncio, json, sys
    sys.path.insert(0, "tests")
    from test_checkpointer_integration import _turns

    url, thread_id, message = sys.argv[1:4]
    print(json.dumps(asyncio.run(_turns(url, thread_id, [message]))))
    """
)


def _turn_in_a_fresh_process(url: str, thread_id: str, message: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _ONE_TURN_IN_A_FRESH_PROCESS, url, thread_id, message],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT / "src")},
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    seen: list[str] = json.loads(result.stdout.strip().splitlines()[-1])
    return seen


async def test_a_conversation_survives_a_process_restart(migrated: str) -> None:
    """Each turn runs in its own Python process: the only thing the second
    process shares with the first is the database."""
    thread = "+5491100024001"

    first = _turn_in_a_fresh_process(migrated, thread, "My name is Ana")
    second = _turn_in_a_fresh_process(migrated, thread, "What is my name?")

    assert first == ["My name is Ana"]
    assert second == ["My name is Ana", "re: My name is Ana", "What is my name?"]


# ---------------------------------------------------------------------------
# Retention: the purge
# ---------------------------------------------------------------------------


async def _thread_rows(url: str, thread_id: str) -> dict[str, int]:
    return {
        table: await _scalar(
            url, f"SELECT count(*) FROM {table} WHERE thread_id = :t", t=thread_id
        )
        for table in _TABLES
    }


async def test_the_purge_deletes_idle_threads_and_keeps_active_ones(
    migrated: str,
) -> None:
    idle, active = "+5491100024002", "+5491100024003"
    await _turns(migrated, idle, ["old"])
    await _turns(migrated, active, ["recent"])
    await _execute(
        migrated,
        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', "
        "to_jsonb('2020-01-01T00:00:00+00:00'::text)) WHERE thread_id = :t",
        t=idle,
    )

    async with open_postgres_checkpointer(
        migrated, pool_size=1, timeout_s=5.0
    ) as saver:
        result = await purge_checkpoints(saver.conn, retention_s=3_600)

    assert result.threads_deleted == 1
    assert set((await _thread_rows(migrated, idle)).values()) == {0}
    assert (await _thread_rows(migrated, active))["checkpoints"] == 1
    assert await _turns(migrated, active, ["again"]) == [
        "recent",
        "re: recent",
        "again",
    ]


async def test_delete_thread_removes_one_thread_on_the_real_saver(
    migrated: str,
) -> None:
    """ADR-011 part 6 (#257): on-demand delete, not by age."""
    gone, kept = "+5491100024010", "+5491100024011"
    await _turns(migrated, gone, ["forget me"])
    await _turns(migrated, kept, ["keep me"])

    async with open_postgres_checkpointer(
        migrated, pool_size=1, timeout_s=5.0
    ) as saver:
        threads = CheckpointThreads(saver)
        exported = await threads.export_thread(gone)
        assert exported is not None
        assert "forget me" in repr(exported["messages"])
        assert await threads.delete_thread(gone) == 1
        assert await threads.delete_thread(gone) == 0

    assert set((await _thread_rows(migrated, gone)).values()) == {0}
    assert (await _thread_rows(migrated, kept))["checkpoints"] >= 1


async def test_the_purge_keeps_only_each_threads_latest_checkpoint(
    migrated: str,
) -> None:
    thread = "+5491100024004"
    await _turns(migrated, thread, ["one", "two", "three"])
    before = await _thread_rows(migrated, thread)
    assert before["checkpoints"] == 3

    async with open_postgres_checkpointer(
        migrated, pool_size=1, timeout_s=5.0
    ) as saver:
        result = await purge_checkpoints(saver.conn, retention_s=None)

    after = await _thread_rows(migrated, thread)
    assert result.threads_deleted == 0
    assert result.checkpoints_pruned == 2
    assert after["checkpoints"] == 1
    assert after["checkpoint_blobs"] < before["checkpoint_blobs"]
    # Pruning drops superseded copies only: the whole history is still there.
    assert await _turns(migrated, thread, ["four"]) == [
        "one",
        "re: one",
        "two",
        "re: two",
        "three",
        "re: three",
        "four",
    ]


# ---------------------------------------------------------------------------
# Failure policy against the real backend
# ---------------------------------------------------------------------------


async def test_a_corrupt_thread_degrades_alone(migrated: str) -> None:
    broken, healthy = "+5491100024005", "+5491100024006"
    await _turns(migrated, broken, ["first"])
    await _turns(migrated, healthy, ["first"])
    await _execute(
        migrated,
        "UPDATE checkpoint_blobs SET type = 'msgpack', blob = '\\x00ff'::bytea "
        "WHERE thread_id = :t",
        t=broken,
    )

    assert await _turns(migrated, broken, ["second"]) == ["second"]
    assert await _turns(migrated, healthy, ["second"]) == [
        "first",
        "re: first",
        "second",
    ]


async def test_a_dropped_database_connection_heals_on_the_next_turn(
    migrated: str,
) -> None:
    thread = "+5491100024007"
    model = _Model(responses=[AIMessage(content="a"), AIMessage(content="b")])
    async with open_postgres_checkpointer(
        migrated, pool_size=1, timeout_s=5.0
    ) as saver:
        agent = AgentRuntime(_runtime(), model, checkpointer=saver)
        await agent.run_turn(
            [HumanMessage(content="one")],
            session_id="s",
            permissions=(),
            thread_id=thread,
        )
        # Kill every other session on this database, the pool's included.
        await _scalar(
            migrated,
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()",
        )
        await agent.run_turn(
            [HumanMessage(content="two")],
            session_id="s",
            permissions=(),
            thread_id=thread,
        )

    assert [m.content for m in model.seen[1:]] == ["one", "a", "two"]  # type: ignore[attr-defined]


async def test_the_lifespan_purger_enforces_retention_on_the_real_saver(
    migrated: str,
) -> None:
    """The exact wiring `create_app`'s lifespan starts: the purger built from
    the Postgres saver and the settings deletes an idle thread on its first
    run, at startup."""
    import asyncio

    from agents_system.config import Settings
    from agents_system.main import _build_checkpoint_purger

    idle = "+5491100024008"
    await _turns(migrated, idle, ["old"])
    await _execute(
        migrated,
        "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', "
        "to_jsonb('2020-01-01T00:00:00+00:00'::text)) WHERE thread_id = :t",
        t=idle,
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        checkpointer_retention_s=3_600,
        checkpointer_purge_interval_s=3_600,
    )

    async with open_postgres_checkpointer(
        migrated, pool_size=1, timeout_s=5.0
    ) as saver:
        purger = _build_checkpoint_purger(saver, settings)
        assert purger is not None
        await purger.start()
        try:
            for _ in range(100):
                if (await _thread_rows(migrated, idle))["checkpoints"] == 0:
                    break
                await asyncio.sleep(0.05)
        finally:
            await purger.stop()

    assert set((await _thread_rows(migrated, idle)).values()) == {0}
