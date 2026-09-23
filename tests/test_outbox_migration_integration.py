"""Integration proof for the durable inbox/outbox migration.

Set OUTBOX_TEST_DATABASE_URL to an isolated disposable PostgreSQL database.
The fixture applies upgrades and downgrades, so it must never target a database
with user data.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentsys.models.base import get_engine
from agentsys.services.outbox import accept_inbound_message

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _require_isolated_database_url() -> str:
    url = os.environ.get("OUTBOX_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "OUTBOX_TEST_DATABASE_URL is required and must name an isolated "
            "throwaway PostgreSQL database; this test runs Alembic downgrade."
        )
    assert url is not None
    return url


def _run_alembic(command: str, url: str) -> None:
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


@pytest.fixture  # type: ignore[untyped-decorator]
async def migrated_engine() -> AsyncIterator[AsyncEngine]:
    url = _require_isolated_database_url()
    _run_alembic("upgrade head", url)
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()
        _run_alembic("downgrade base", url)


async def test_upgrade_creates_inbox_outbox_and_pending_indexes(
    migrated_engine: AsyncEngine,
) -> None:
    async with migrated_engine.connect() as conn:
        tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema()"
                    )
                )
            ).scalars()
        )
        indexes = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() AND tablename = 'outbox_work'"
                    )
                )
            ).scalars()
        )

    assert {"webhook_inbox", "outbox_work"}.issubset(tables)
    assert {"ix_outbox_work_pending", "ix_outbox_work_expired_lease"}.issubset(indexes)


async def test_concurrent_same_meta_id_creates_one_inbox_and_one_work_row(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
    meta_message_id = f"wamid.concurrent.{uuid.uuid4()}"

    async def accept() -> bool:
        async with session_factory() as session:
            result = await accept_inbound_message(
                session,
                meta_message_id=meta_message_id,
                payload={"text": "hello"},
            )
            return bool(result.duplicate)

    duplicates = await asyncio.gather(accept(), accept())

    assert sorted(duplicates) == [False, True]
    async with migrated_engine.connect() as conn:
        inbox_count = await conn.scalar(
            text(
                "SELECT count(*) FROM webhook_inbox "
                "WHERE meta_message_id = :meta_message_id"
            ),
            {"meta_message_id": meta_message_id},
        )
        work_count = await conn.scalar(
            text(
                "SELECT count(*) FROM outbox_work work "
                "JOIN webhook_inbox inbox ON inbox.id = work.inbound_message_id "
                "WHERE inbox.meta_message_id = :meta_message_id"
            ),
            {"meta_message_id": meta_message_id},
        )

    assert inbox_count == 1
    assert work_count == 1

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM outbox_work USING webhook_inbox "
                "WHERE webhook_inbox.id = outbox_work.inbound_message_id "
                "AND webhook_inbox.meta_message_id = :meta_message_id"
            ),
            {"meta_message_id": meta_message_id},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE meta_message_id = :meta_message_id"),
            {"meta_message_id": meta_message_id},
        )


async def test_downgrade_refuses_pending_or_leased_outbox_work(
    migrated_engine: AsyncEngine,
) -> None:
    pending_inbox_id = str(uuid.uuid4())
    leased_inbox_id = str(uuid.uuid4())
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_inbox (id, meta_message_id, payload) VALUES "
                "(CAST(:id AS uuid), :meta_message_id, '{}'::jsonb)"
            ),
            {"id": pending_inbox_id, "meta_message_id": "wamid.pending"},
        )
        await conn.execute(
            text(
                "INSERT INTO webhook_inbox (id, meta_message_id, payload) VALUES "
                "(CAST(:id AS uuid), :meta_message_id, '{}'::jsonb)"
            ),
            {"id": leased_inbox_id, "meta_message_id": "wamid.leased"},
        )
        await conn.execute(
            text(
                "INSERT INTO outbox_work (id, inbound_message_id) VALUES "
                "(CAST(:id AS uuid), CAST(:inbound_message_id AS uuid))"
            ),
            {"id": str(uuid.uuid4()), "inbound_message_id": pending_inbox_id},
        )
        await conn.execute(
            text(
                "INSERT INTO outbox_work (id, inbound_message_id, lease_expires_at) "
                "VALUES (CAST(:id AS uuid), CAST(:inbound_message_id AS uuid), "
                "now() + INTERVAL '5 minutes')"
            ),
            {"id": str(uuid.uuid4()), "inbound_message_id": leased_inbox_id},
        )

    url = _require_isolated_database_url()
    with pytest.raises(AssertionError, match="pending or leased outbox work"):
        _run_alembic("downgrade 001", url)

    async with migrated_engine.connect() as conn:
        remaining_work = await conn.scalar(text("SELECT count(*) FROM outbox_work"))
    assert remaining_work == 2

    async with migrated_engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_work"))
        await conn.execute(text("DELETE FROM webhook_inbox"))


async def test_downgrade_to_001_removes_only_new_inbox_outbox_tables(
    migrated_engine: AsyncEngine,
) -> None:
    url = _require_isolated_database_url()
    _run_alembic("downgrade 001", url)

    async with migrated_engine.connect() as conn:
        tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema()"
                    )
                )
            ).scalars()
        )

    assert "webhook_inbox" not in tables
    assert "outbox_work" not in tables
    assert "audit_event" in tables

    _run_alembic("upgrade head", url)
