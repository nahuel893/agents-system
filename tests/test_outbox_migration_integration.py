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
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentsys.models.base import get_engine
from agentsys.models.outbox import OutboxWork
from agentsys.services.outbox import (
    MAX_OUTBOX_ATTEMPTS,
    OutboxLeaseLostError,
    accept_inbound_message,
    claim_available_outbox_work,
    persist_outbound_intent,
    record_outbox_failure,
)

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


@pytest_asyncio.fixture
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
    assert {
        "ix_outbox_work_pending",
        "ix_outbox_work_expired_lease",
        "ix_outbox_work_ready_recoverable",
        "ix_outbox_work_expired_recoverable",
    }.issubset(indexes)

    async with migrated_engine.connect() as conn:
        columns = set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'outbox_work'"
                    )
                )
            ).scalars()
        )
    assert {
        "attempt_count",
        "lease_owner",
        "failed_at",
        "last_error",
        "outbound_body",
        "outbound_send_key",
    }.issubset(columns)


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
        revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        indexes = set(
            (
                await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = 'outbox_work'"
                    )
                )
            ).scalars()
        )
    assert remaining_work == 2
    assert revision == "003"
    assert {
        "ix_outbox_work_ready_recoverable",
        "ix_outbox_work_expired_recoverable",
    }.issubset(indexes)

    async with migrated_engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_work"))
        await conn.execute(text("DELETE FROM webhook_inbox"))


async def test_concurrent_claims_skip_a_live_lease_after_the_first_commit(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with session_factory() as session:
        acceptance = await accept_inbound_message(
            session,
            meta_message_id=f"wamid.claim.{uuid.uuid4()}",
            payload={"text": "claim me"},
        )

    async def claim(worker_id: str) -> list[OutboxWork]:
        async with session_factory() as session:
            return await claim_available_outbox_work(
                session,
                worker_id=worker_id,
                limit=1,
            )

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sorted([len(first), len(second)]) == [0, 1]

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM outbox_work WHERE inbound_message_id = CAST(:id AS uuid)"
            ),
            {"id": str(acceptance.inbound_message_id)},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE id = CAST(:id AS uuid)"),
            {"id": str(acceptance.inbound_message_id)},
        )


async def test_terminal_failure_commits_outbox_state_and_audit_together(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with session_factory() as session:
        acceptance = await accept_inbound_message(
            session,
            meta_message_id=f"wamid.terminal.{uuid.uuid4()}",
            payload={"text": "fail me"},
        )
    async with session_factory() as session:
        claimed = await claim_available_outbox_work(
            session,
            worker_id="worker-a",
            limit=1,
        )
        assert len(claimed) == 1
        work = claimed[0]
        assert work.inbound_message_id == acceptance.inbound_message_id
        # Model the final live attempt, not an unleased row: fenced failures
        # must reject a worker that never held the lease.
        work.attempt_count = MAX_OUTBOX_ATTEMPTS
        await record_outbox_failure(
            session,
            work=work,
            worker_id="worker-a",
            error="permanent provider failure",
        )

    async with migrated_engine.connect() as conn:
        failed_at = await conn.scalar(
            text(
                "SELECT failed_at FROM outbox_work WHERE inbound_message_id = CAST(:id AS uuid)"
            ),
            {"id": str(acceptance.inbound_message_id)},
        )
        audit_count = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_event "
                "WHERE correlation_id = :correlation_id "
                "AND event_type = 'webhook_delivery_terminal_failure'"
            ),
            {"correlation_id": f"outbox:{work.id}"},
        )
    assert failed_at is not None
    assert audit_count == 1

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM audit_event WHERE correlation_id = :correlation_id"),
            {"correlation_id": f"outbox:{work.id}"},
        )
        await conn.execute(
            text(
                "DELETE FROM outbox_work WHERE inbound_message_id = CAST(:id AS uuid)"
            ),
            {"id": str(acceptance.inbound_message_id)},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE id = CAST(:id AS uuid)"),
            {"id": str(acceptance.inbound_message_id)},
        )


async def test_stale_worker_cannot_mutate_work_reclaimed_by_another_worker(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with session_factory() as session:
        acceptance = await accept_inbound_message(
            session,
            meta_message_id=f"wamid.fenced.{uuid.uuid4()}",
            payload={"text": "fence me"},
        )
        stale_work = (
            await claim_available_outbox_work(
                session,
                worker_id="worker-a",
                limit=1,
            )
        )[0]

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE outbox_work SET lease_expires_at = clock_timestamp() - "
                "INTERVAL '1 second' WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(stale_work.id)},
        )

    async with session_factory() as session:
        recovered = await claim_available_outbox_work(
            session,
            worker_id="worker-b",
            limit=1,
        )
    assert [work.id for work in recovered] == [stale_work.id]

    async with session_factory() as session:
        with pytest.raises(OutboxLeaseLostError):
            await persist_outbound_intent(
                session,
                work=stale_work,
                worker_id="worker-a",
                body={"text": "stale write"},
                send_key="turn-a",
            )
        with pytest.raises(OutboxLeaseLostError):
            await record_outbox_failure(
                session,
                work=stale_work,
                worker_id="worker-a",
                error="stale failure",
            )

    async with migrated_engine.connect() as conn:
        state = await conn.execute(
            text(
                "SELECT lease_owner, outbound_body, last_error FROM outbox_work "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(stale_work.id)},
        )
        lease_owner, outbound_body, last_error = state.one()
    assert lease_owner == "worker-b"
    assert outbound_body is None
    assert last_error is None

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox_work WHERE id = CAST(:id AS uuid)"),
            {"id": str(stale_work.id)},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE id = CAST(:id AS uuid)"),
            {"id": str(acceptance.inbound_message_id)},
        )


async def test_expired_max_attempt_claim_is_terminalized_with_audit(
    migrated_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(migrated_engine, expire_on_commit=False)
    async with session_factory() as session:
        acceptance = await accept_inbound_message(
            session,
            meta_message_id=f"wamid.max-attempt.{uuid.uuid4()}",
            payload={"text": "cap me"},
        )
        last_claim: OutboxWork | None = None
        for attempt in range(MAX_OUTBOX_ATTEMPTS):
            claimed = await claim_available_outbox_work(
                session,
                worker_id=f"worker-{attempt}",
                limit=1,
            )
            assert len(claimed) == 1
            await session.execute(
                text(
                    "UPDATE outbox_work SET lease_expires_at = clock_timestamp() - "
                    "INTERVAL '1 second' WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(claimed[0].id)},
            )
            await session.commit()
            last_claim = claimed[0]
        assert last_claim is not None
        work_id = last_claim.id

    async with session_factory() as session:
        recovered = await claim_available_outbox_work(
            session,
            worker_id="recovery-worker",
            limit=1,
        )
    assert recovered == []

    async with migrated_engine.connect() as conn:
        failed_at = await conn.scalar(
            text("SELECT failed_at FROM outbox_work WHERE id = CAST(:id AS uuid)"),
            {"id": str(work_id)},
        )
        audit_count = await conn.scalar(
            text(
                "SELECT count(*) FROM audit_event "
                "WHERE correlation_id = :correlation_id "
                "AND event_type = 'webhook_delivery_terminal_failure'"
            ),
            {"correlation_id": f"outbox:{work_id}"},
        )
    assert failed_at is not None
    assert audit_count == 1

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM audit_event WHERE correlation_id = :correlation_id"),
            {"correlation_id": f"outbox:{work_id}"},
        )
        await conn.execute(
            text("DELETE FROM outbox_work WHERE id = CAST(:id AS uuid)"),
            {"id": str(work_id)},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE id = CAST(:id AS uuid)"),
            {"id": str(acceptance.inbound_message_id)},
        )


async def test_downgrade_003_refuses_recoverable_state_without_partial_schema_loss(
    migrated_engine: AsyncEngine,
) -> None:
    work_id = str(uuid.uuid4())
    inbox_id = str(uuid.uuid4())
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_inbox (id, meta_message_id, payload) VALUES "
                "(CAST(:id AS uuid), :meta_message_id, '{}'::jsonb)"
            ),
            {"id": inbox_id, "meta_message_id": f"wamid.downgrade.{uuid.uuid4()}"},
        )
        await conn.execute(
            text(
                "INSERT INTO outbox_work (id, inbound_message_id, outbound_send_key) "
                "VALUES (CAST(:id AS uuid), CAST(:inbound_message_id AS uuid), :send_key)"
            ),
            {"id": work_id, "inbound_message_id": inbox_id, "send_key": "turn-guard"},
        )

    url = _require_isolated_database_url()
    with pytest.raises(AssertionError, match="recoverable outbox state"):
        _run_alembic("downgrade 002", url)

    async with migrated_engine.connect() as conn:
        revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        column_exists = await conn.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'outbox_work' "
                "AND column_name = 'outbound_send_key')"
            )
        )
    assert revision == "003"
    assert column_exists is True

    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM outbox_work WHERE id = CAST(:id AS uuid)"),
            {"id": work_id},
        )
        await conn.execute(
            text("DELETE FROM webhook_inbox WHERE id = CAST(:id AS uuid)"),
            {"id": inbox_id},
        )


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
