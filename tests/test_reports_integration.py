"""Integration tests for BI reports against the real Postgres database,
executed through the dedicated `bi_readonly` role (AD-3).

Skipped by default - opt-in with::

    BI_DATABASE_URL=postgresql+asyncpg://bi_readonly:<password>@localhost:5432/acme \\
        uv run pytest -m integration tests/test_reports_integration.py -v

Requires the seeded demo dataset (``uv run python scripts/seed_db.py`` or
equivalent) and the `bi_readonly` role already created in Postgres
(see architecture/bi-readonly-db-role). Skips with a clear reason rather than
erroring when BI_DATABASE_URL is unset, since not every environment running
`-m integration` will have this specific credential exported.

Two tests in this file are the exception to "requires the ACME demo
dataset": ``test_audit_event_report_spec_returns_expected_shape`` and
``test_bi_readonly_engine_refuses_writes`` exercise `run_report` and the
read-only role against ``audit_event`` - platform-owned, already
Alembic-migrated - and seed their own single row via ``admin_engine``
(``DATABASE_URL``, full access). This is deliberate: `clients` is scheduled
to leave this repository (`extract-acme-client-repo`), and the read-only
role's ONLY test of a security property must not be anchored to a table
that is about to disappear (see platform-repo-boundary spec, "bi-readonly
runs against a platform-owned fixture").
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentsys.connectors import acme_reports
from agentsys.models.audit_event import map_to_audit_event
from agentsys.models.base import get_engine
from agentsys.services.reports import HARD_ROW_CEILING, ParamSpec, ReportSpec, run_report

pytestmark = pytest.mark.integration


def _require_bi_database_url() -> str:
    url = os.environ.get("BI_DATABASE_URL")
    if not url:
        pytest.skip(
            "BI_DATABASE_URL not set - export the bi_readonly connection string "
            "to run these tests."
        )
    return url


def _require_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "DATABASE_URL not set - export the full-access connection string "
            "to seed platform-owned fixture data for these tests."
        )
    return url


@pytest.fixture
async def bi_engine() -> AsyncIterator[AsyncEngine]:
    url = _require_bi_database_url()
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """Full-access engine used ONLY to seed platform-owned fixture data.

    Never used to exercise the read-only role itself - that is exclusively
    `bi_engine`'s job. Backed by `DATABASE_URL`, which every CI job running
    this file already exports (it is how `scripts/seed_demo_data.py` and
    `alembic upgrade head` connect).
    """
    url = _require_database_url()
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_audit_event_correlation_id(admin_engine: AsyncEngine) -> str:
    """Insert one deterministic, platform-owned `audit_event` row.

    Returns its unique `correlation_id`, so the report spec below can select
    exactly this run's row regardless of whatever other audit_event rows
    already exist (e.g. from a developer's local database or other test
    runs) - no ACME fixture, no shared demo dataset.
    """
    correlation_id = f"bi-readonly-fixture-{uuid.uuid4()}"
    row = map_to_audit_event(
        {
            "event_id": uuid.uuid4(),
            "correlation_id": correlation_id,
            "sequence": 1,
            "event_type": "bi_readonly_fixture_probe",
            "role": "test",
            "payload": {"source": "test_reports_integration"},
        }
    )
    session_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(row)
        await session.commit()
    return correlation_id


_AUDIT_EVENT_SAMPLE_SQL = text(
    """
    SELECT event_type, role, tool_name, occurred_at
    FROM audit_event
    WHERE correlation_id = :correlation_id
    ORDER BY sequence
    LIMIT :limit
    """
).bindparams(bindparam("correlation_id", type_=String()))

_AUDIT_EVENT_SAMPLE_SPEC = ReportSpec(
    name="audit_event_sample",
    description=(
        "Test-only report spec proving `run_report` end-to-end against a "
        "platform-owned table - references no client-owned table "
        "(platform-repo-boundary: 'the run_report path stays covered by a "
        "report spec that references no client-owned table')."
    ),
    sql=_AUDIT_EVENT_SAMPLE_SQL,
    params=(
        ParamSpec(
            name="correlation_id",
            type=str,
            required=True,
            description="correlation_id of the seeded audit_event row(s).",
        ),
        ParamSpec(
            name="limit",
            type=int,
            default=10,
            minimum=1,
            description="Maximum rows to return.",
        ),
    ),
)


async def test_ventas_por_mes_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine,
) -> None:
    spec = acme_reports.CATALOG["ventas_por_mes"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 50})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"month", "order_count", "revenue"}
    assert result["meta"]["cancelled_included"] is False


async def test_top_clientes_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = acme_reports.CATALOG["top_clientes"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 5})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"client_name", "zone", "order_count", "revenue"}


async def test_ventas_por_zona_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = acme_reports.CATALOG["ventas_por_zona"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 20})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"zone", "order_count", "revenue", "avg_ticket"}


async def test_ventas_por_tipo_negocio_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = acme_reports.CATALOG["ventas_por_tipo_negocio"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 20})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"business_type", "order_count", "revenue", "avg_ticket"}


async def test_top_productos_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = acme_reports.CATALOG["top_productos"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 10})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"sku", "description", "total_quantity", "revenue"}


async def test_resumen_estados_returns_all_three_seeded_statuses(
    bi_engine: AsyncEngine,
) -> None:
    spec = acme_reports.CATALOG["resumen_estados"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 10})

    statuses = {row["status"] for row in result["rows"]}
    assert statuses == {"confirmed", "pending", "cancelled"}


async def test_row_limit_is_clamped_to_hard_ceiling(bi_engine: AsyncEngine) -> None:
    spec = acme_reports.CATALOG["top_productos"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 999_999})

    assert result["row_count"] <= HARD_ROW_CEILING


async def test_excluding_cancelled_yields_fewer_or_equal_orders_than_all(
    bi_engine: AsyncEngine,
) -> None:
    """Proves the status filter actually changes the answer - the whole
    point of AD's "never silently pick one" business-semantics rule."""
    spec = acme_reports.CATALOG["ventas_por_mes"]

    default_result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 50})
    all_result = await run_report(
        bi_engine, spec, {"months_back": 24, "limit": 50, "status": "all"}
    )

    default_orders = sum(row["order_count"] for row in default_result["rows"])
    all_orders = sum(row["order_count"] for row in all_result["rows"])

    assert all_orders >= default_orders
    assert default_result["meta"]["statuses_included"] != all_result["meta"]["statuses_included"]


async def test_audit_event_report_spec_returns_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """The `run_report` path stays covered by a report spec that references
    no client-owned table (platform-repo-boundary: "bi-readonly runs against
    a platform-owned fixture")."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_SAMPLE_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 10},
    )

    assert result["row_count"] == 1
    row = result["rows"][0]
    assert set(row.keys()) == {"event_type", "role", "tool_name", "occurred_at"}
    assert row["event_type"] == "bi_readonly_fixture_probe"
    assert row["role"] == "test"


async def test_bi_readonly_engine_refuses_writes(bi_engine: AsyncEngine) -> None:
    """Defence in depth (AD-3): even a mutating statement sent through this
    tool's OWN engine must be refused at the database role level.

    Repointed at `audit_event` (platform-owned) instead of `clients`
    (ACME-owned, scheduled to leave this repository via
    `extract-acme-client-repo`) - see platform-repo-boundary spec,
    "bi-readonly runs against a platform-owned fixture". The INSERT below
    supplies every NOT NULL column `audit_event` has, so the ONLY reason it
    can fail is the read-only role - never a constraint violation standing
    in for the property this test exists to prove.
    """
    with pytest.raises(Exception) as exc:
        async with bi_engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_event "
                    "(event_id, correlation_id, sequence, event_type, payload) "
                    "VALUES ("
                    "'00000000-0000-0000-0000-000000000000', "
                    "'should-not-persist', 0, 'should-not-persist', '{}'::jsonb"
                    ")"
                )
            )
    assert "read-only" in str(exc.value).lower()
