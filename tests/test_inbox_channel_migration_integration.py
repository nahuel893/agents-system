"""Integration proof for migration 006: the inbox names each row's channel (#22).

Set OUTBOX_TEST_DATABASE_URL to an isolated disposable PostgreSQL database.
These tests apply upgrades and downgrades, so they must never target a
database with user data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agents_system.models.base import get_engine
from agents_system.services.outbox import accept_inbound_message

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _url() -> str:
    url = os.environ.get("OUTBOX_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "OUTBOX_TEST_DATABASE_URL is required and must name an isolated "
            "throwaway PostgreSQL database; this test runs Alembic downgrade."
        )
    assert url is not None
    return url


def _alembic(command: str, url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        check=False,
        text=True,
    )


def _run_alembic(command: str, url: str) -> None:
    result = _alembic(command, url)
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {command} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    url = _url()
    _run_alembic("upgrade head", url)
    built = get_engine(url)
    try:
        yield built
    finally:
        async with built.begin() as conn:
            await conn.execute(text("DELETE FROM outbox_work"))
            await conn.execute(text("DELETE FROM webhook_inbox"))
        await built.dispose()
        _run_alembic("downgrade base", url)


async def test_a_row_accepted_before_006_is_a_whatsapp_row_after_it() -> None:
    url = _url()
    _run_alembic("upgrade 005", url)
    engine = get_engine(url)
    inbound_id = uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO webhook_inbox (id, meta_message_id, payload) "
                    "VALUES (:id, 'wamid.before', CAST(:payload AS jsonb))"
                ),
                {"id": inbound_id, "payload": json.dumps({"entry": []})},
            )
        _run_alembic("upgrade head", url)
        async with engine.connect() as conn:
            channel = await conn.scalar(
                text("SELECT channel FROM webhook_inbox WHERE id = :id"),
                {"id": inbound_id},
            )
            nullable = await conn.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'webhook_inbox' AND column_name = 'channel'"
                )
            )
        assert channel == "whatsapp"
        assert nullable == "NO"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM webhook_inbox"))
        await engine.dispose()
        _run_alembic("downgrade base", url)


async def test_the_same_native_id_on_two_channels_is_two_messages(
    engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        whatsapp = await accept_inbound_message(
            session,
            meta_message_id="m-1",
            payload={"entry": []},
            conversation_key="5491100000001",
        )
    async with sessions() as session:
        fake = await accept_inbound_message(
            session,
            meta_message_id="m-1",
            payload={"messages": []},
            conversation_key="5491100000001",
            channel="fake",
        )
    async with sessions() as session:
        fake_again = await accept_inbound_message(
            session,
            meta_message_id="m-1",
            payload={"messages": []},
            conversation_key="5491100000001",
            channel="fake",
        )

    assert whatsapp.duplicate is False
    assert fake.duplicate is False
    assert fake_again.duplicate is True
    assert fake_again.inbound_message_id == fake.inbound_message_id
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT wi.channel, wi.meta_message_id, ow.conversation_key "
                    "FROM webhook_inbox wi "
                    "JOIN outbox_work ow ON ow.inbound_message_id = wi.id "
                    "ORDER BY wi.channel"
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        ("fake", "fake:m-1", "fake:5491100000001"),
        ("whatsapp", "m-1", "5491100000001"),
    ]


async def test_downgrade_006_refuses_while_another_channel_has_rows(
    engine: AsyncEngine,
) -> None:
    url = _url()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await accept_inbound_message(
            session,
            meta_message_id="m-1",
            payload={"messages": []},
            conversation_key="x",
            channel="fake",
        )

    refused = _alembic("downgrade 005", url)

    assert refused.returncode != 0
    assert "Refusing to downgrade 006" in refused.stderr
    async with engine.connect() as conn:
        still_there = await conn.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'webhook_inbox' AND column_name = 'channel'"
            )
        )
    assert still_there == 1

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_work"))
        await conn.execute(text("DELETE FROM webhook_inbox"))
    _run_alembic("downgrade 005", url)
    async with engine.connect() as conn:
        gone = await conn.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'webhook_inbox' AND column_name = 'channel'"
            )
        )
    assert gone == 0
    _run_alembic("upgrade head", url)
