"""Integration tests for BI reports against the real Postgres database,
executed through the dedicated `bi_readonly` role (AD-3).

Skipped by default - opt-in with::

    BI_DATABASE_URL=postgresql+asyncpg://bi_readonly:<password>@localhost:5432/badie \\
        uv run pytest -m integration tests/test_reports_integration.py -v

Requires the seeded demo dataset (``uv run python scripts/seed_db.py`` or
equivalent) and the `bi_readonly` role already created in Postgres
(see architecture/bi-readonly-db-role). Skips with a clear reason rather than
erroring when BI_DATABASE_URL is unset, since not every environment running
`-m integration` will have this specific credential exported.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.connectors import badie_reports
from agentsys.models.base import get_engine
from agentsys.services.reports import HARD_ROW_CEILING, run_report

pytestmark = pytest.mark.integration


def _require_bi_database_url() -> str:
    url = os.environ.get("BI_DATABASE_URL")
    if not url:
        pytest.skip(
            "BI_DATABASE_URL not set - export the bi_readonly connection string "
            "to run these tests."
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


async def test_ventas_por_mes_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine,
) -> None:
    spec = badie_reports.CATALOG["ventas_por_mes"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 50})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"month", "order_count", "revenue"}
    assert result["meta"]["cancelled_included"] is False


async def test_top_clientes_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = badie_reports.CATALOG["top_clientes"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 5})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"client_name", "zone", "order_count", "revenue"}


async def test_ventas_por_zona_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = badie_reports.CATALOG["ventas_por_zona"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 20})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"zone", "order_count", "revenue", "avg_ticket"}


async def test_ventas_por_tipo_negocio_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = badie_reports.CATALOG["ventas_por_tipo_negocio"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 20})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"business_type", "order_count", "revenue", "avg_ticket"}


async def test_top_productos_returns_rows(bi_engine: AsyncEngine) -> None:
    spec = badie_reports.CATALOG["top_productos"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 10})

    assert result["row_count"] > 0
    row = result["rows"][0]
    assert set(row.keys()) == {"sku", "description", "total_quantity", "revenue"}


async def test_resumen_estados_returns_all_three_seeded_statuses(
    bi_engine: AsyncEngine,
) -> None:
    spec = badie_reports.CATALOG["resumen_estados"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 10})

    statuses = {row["status"] for row in result["rows"]}
    assert statuses == {"confirmed", "pending", "cancelled"}


async def test_row_limit_is_clamped_to_hard_ceiling(bi_engine: AsyncEngine) -> None:
    spec = badie_reports.CATALOG["top_productos"]
    result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 999_999})

    assert result["row_count"] <= HARD_ROW_CEILING


async def test_excluding_cancelled_yields_fewer_or_equal_orders_than_all(
    bi_engine: AsyncEngine,
) -> None:
    """Proves the status filter actually changes the answer - the whole
    point of AD's "never silently pick one" business-semantics rule."""
    spec = badie_reports.CATALOG["ventas_por_mes"]

    default_result = await run_report(bi_engine, spec, {"months_back": 24, "limit": 50})
    all_result = await run_report(
        bi_engine, spec, {"months_back": 24, "limit": 50, "status": "all"}
    )

    default_orders = sum(row["order_count"] for row in default_result["rows"])
    all_orders = sum(row["order_count"] for row in all_result["rows"])

    assert all_orders >= default_orders
    assert default_result["meta"]["statuses_included"] != all_result["meta"]["statuses_included"]


async def test_bi_readonly_engine_refuses_writes(bi_engine: AsyncEngine) -> None:
    """Defence in depth (AD-3): even a mutating statement sent through this
    tool's OWN engine must be refused at the database role level."""
    from sqlalchemy import text

    with pytest.raises(Exception) as exc:
        async with bi_engine.connect() as conn:
            await conn.execute(
                text("INSERT INTO clients (name) VALUES ('should-not-persist')")
            )
    assert "read-only" in str(exc.value).lower()
