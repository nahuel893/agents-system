"""The Postgres usage ledger: the real migration, row-level security, the caps (#252).

Needs ``DATABASE_URL`` (an admin URL, used only to provision a throwaway
schema and a non-superuser role) and skips without it.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.scope import assert_subject_isolation
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.services.usage_postgres import PostgresUsageLedger
from agents_system.subject_data import SubjectRef
from agents_system.usage import BudgetCaps, UsageRecord, reached_cap

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "009_subject_usage.py"
)


def _migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_009", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def ledger() -> AsyncIterator[PostgresUsageLedger]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    suffix = uuid.uuid4().hex[:8]
    role, schema, pw = f"us_app_{suffix}", f"us_{suffix}", "pw" + suffix
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
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.subject_usage "
                f"TO {role}"
            )
        )
    app = create_async_engine(
        admin_url.set(username=role, password=pw),
        connect_args={"server_settings": {"search_path": schema}},
    )
    provider = PostgresScopeProvider(app)
    await provider.verify()
    try:
        yield PostgresUsageLedger(provider)
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


def _run_upgrade(context: MigrationContext, migration: object) -> None:
    with Operations.context(context):
        migration.upgrade()  # type: ignore[attr-defined]


async def test_an_unfiltered_read_sees_only_the_scoped_subject(
    ledger: PostgresUsageLedger,
) -> None:
    async def write(scope: object, value: str) -> None:
        await ledger.record(
            UsageRecord(
                runtime_id=scope.runtime_id,  # type: ignore[attr-defined]
                subject_key=scope.subject_key,  # type: ignore[attr-defined]
                actor=value,
            )
        )

    async def read_all(scope: object) -> list[str]:
        records = await ledger.query(
            scope.runtime_id,  # type: ignore[attr-defined]
            scope.subject_key,  # type: ignore[attr-defined]
        )
        return [r.actor or "" for r in records]

    await assert_subject_isolation(
        ledger._provider,  # type: ignore[arg-type]
        write=write,
        read_all=read_all,
        runtime_id="rt",
    )


async def test_totals_flag_unpriced_turns_and_a_cap_is_reached(
    ledger: PostgresUsageLedger,
) -> None:
    now = datetime.now(UTC)
    for cost in (0.4, 0.4, None):
        await ledger.record(
            UsageRecord(
                runtime_id="rt",
                subject_key="participant:1",
                total_tokens=10,
                cost_usd=cost,
                occurred_at=now,
            )
        )
    await ledger.record(
        UsageRecord(runtime_id="rt", subject_key="participant:2", total_tokens=999)
    )
    totals = await ledger.totals(
        "rt", "participant:1", since=now.replace(hour=0, minute=0, second=0)
    )
    assert (totals.tokens, totals.turns, totals.unpriced_turns) == (30, 3, 1)
    assert totals.cost_usd == pytest.approx(0.8)
    reached = await reached_cap(
        ledger, BudgetCaps(daily_usd=0.5), "rt", "participant:1"
    )
    assert reached is not None and reached[0] == "daily_usd"
    assert (
        await reached_cap(ledger, BudgetCaps(daily_usd=0.5), "rt", "participant:2")
        is None
    )


async def test_export_and_delete_cover_one_subject(
    ledger: PostgresUsageLedger,
) -> None:
    for key in ("participant:1", "participant:2"):
        await ledger.record(
            UsageRecord(runtime_id="rt", subject_key=key, total_tokens=5)
        )
    ref = SubjectRef("rt", "participant:1")
    assert len(await ledger.export(ref)) == 1
    assert await ledger.delete(ref) == 1
    assert await ledger.export(ref) == []
    assert len(await ledger.export(SubjectRef("rt", "participant:2"))) == 1
