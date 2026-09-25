"""The portable catalog, run against a company that is shaped differently.

`tests/test_sales_reports_contract.py` proves the SQL only names contract
views. That is a static check, and a static check cannot tell you the views
carry the right data — a status mapping that drops a value, or a computed line
total that disagrees with the invoice header, both pass it and both produce
confidently wrong numbers.

So these run the real reports against the real demo database, and assert
figures derived independently from `demo/company/03_seed.sql`:

  360 invoices, one every 36 hours.
  Every 10th is 'anulada'                     -> 36 cancelled
  Every 5th that is not already anulada       -> 36 pending
  The rest                                    -> 288 confirmed

None of those numbers is read back from the module under test.

Marked `integration` because it needs a live Postgres. It is run by the
`demo-reports` CI job, which loads the demo company first — a marker on its
own is not coverage.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agents_system.connectors.sales_reports import CATALOG, UNMAPPED_STATUS
from agents_system.services.reports import run_report

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo"
)

#: Straight from `03_seed.sql`, recomputed here rather than imported.
SEEDED_SALES = 360
SEEDED_CANCELLED = 36
SEEDED_PENDING = 36
SEEDED_CONFIRMED = 288
SEEDED_CUSTOMERS = 12
SEEDED_ZONES = 4
SEEDED_SEGMENTS = 3
SEEDED_ARTICLES = 20

#: 360 invoices * 36h = 540 days of history, so 24 months (720 days) covers
#: every one of them. Anything shorter would make these totals clock-dependent.
WHOLE_HISTORY = 24


def _demo_url() -> str:
    return os.getenv("DEMO_DATABASE_URL", _DEFAULT_URL)


@pytest.fixture(scope="module")
def demo_database() -> str:
    """Load the demo company, or skip if no database is reachable.

    Loading here rather than assuming a pre-loaded database keeps the test
    self-contained: the seed is deterministic, so a reload is idempotent as
    far as every assertion below is concerned.
    """
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "demo" / "load_demo_company.py")],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"demo company unavailable: {result.stderr.strip()[:300]}")
    return _demo_url()


@pytest.fixture
async def engine(demo_database: str) -> Any:
    engine = create_async_engine(demo_database)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _report(engine: Any, name: str, **params: Any) -> dict[str, Any]:
    return await run_report(engine, CATALOG[name], params)


# --- The status mapping, which is the easiest thing to get silently wrong ---


async def test_status_summary_recovers_the_seeded_status_mix(engine: Any) -> None:
    """The company stores facturada/pendiente/anulada; the view maps them.

    A mapping that dropped a value would not error — the rows would simply be
    absent from every filtered report. This is where that shows up.
    """
    result = await _report(
        engine, "status_summary", months_back=WHOLE_HISTORY, limit=10
    )

    counts = {row["status"]: row["sale_count"] for row in result["rows"]}

    assert counts == {
        "confirmed": SEEDED_CONFIRMED,
        "pending": SEEDED_PENDING,
        "cancelled": SEEDED_CANCELLED,
    }
    assert sum(counts.values()) == SEEDED_SALES


async def test_no_sale_falls_outside_the_canonical_vocabulary(engine: Any) -> None:
    """Every row must land on one of the three canonical statuses.

    An unmapped company status would surface here as a fourth key.
    """
    result = await _report(
        engine, "status_summary", months_back=WHOLE_HISTORY, limit=50
    )

    assert {row["status"] for row in result["rows"]} <= {
        "confirmed",
        "pending",
        "cancelled",
    }


@pytest.fixture
async def sale_with_an_unmapped_status(engine: Any) -> Any:
    """Insert one invoice whose `estado` the view's CASE does not recognise.

    The demo seed only ever writes the three words the view maps, so the
    unmapped path is unreachable from the seed alone — which is exactly why it
    stayed broken: `ELSE 'cancelled'` had no test that could see it. This
    fixture supplies the missing row, then removes it, so the surrounding
    tests' exact counts stay exact.
    """
    sale_id = 999_001
    amount = "12345.00"
    async with engine.begin() as conn:
        customer = await conn.execute(
            text("SELECT MIN(nro_cliente) FROM padron_clientes")
        )
        await conn.execute(
            text(
                "INSERT INTO facturas "
                "(nro_factura, nro_cliente, fecha_emision, estado, importe_total) "
                "VALUES (:id, :cliente, now(), 'en_proceso', :amount)"
            ),
            {"id": sale_id, "cliente": customer.scalar_one(), "amount": amount},
        )
    try:
        yield {"sale_id": sale_id, "amount": amount}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM facturas WHERE nro_factura = :id"),
                {"id": sale_id},
            )


async def test_an_unmapped_source_status_is_not_reported_as_a_cancellation(
    engine: Any, sale_with_an_unmapped_status: dict[str, Any]
) -> None:
    """THE regression. 'en_proceso' must not be counted as a cancelled sale.

    The view used to end `ELSE 'cancelled'`, on the reasoning that keeping an
    unrecognized status out of revenue is conservative. It is — for revenue. But
    `status_summary` does not filter by status, so this row came back as a
    CANCELLATION that no invoice in the company ever recorded, and the agent
    would report that figure with full confidence.
    """
    result = await _report(
        engine, "status_summary", months_back=WHOLE_HISTORY, limit=50
    )

    counts = {row["status"]: row["sale_count"] for row in result["rows"]}

    assert counts.get(UNMAPPED_STATUS) == 1
    assert counts["cancelled"] == SEEDED_CANCELLED


async def test_an_unmapped_status_is_excluded_from_every_revenue_figure(
    engine: Any, sale_with_an_unmapped_status: dict[str, Any]
) -> None:
    """Outside the vocabulary means outside every status-filtered report.

    This is the half the old mapping got right, and it has to survive the fix:
    an unmapped row must not reach revenue just because it is no longer called
    cancelled.
    """
    result = await _report(engine, "sales_by_zone", months_back=WHOLE_HISTORY, limit=50)

    counted = sum(row["sale_count"] for row in result["rows"])

    assert counted == SEEDED_CONFIRMED + SEEDED_PENDING
    assert Decimalish(sale_with_an_unmapped_status["amount"]) not in [
        Decimalish(row["revenue"]) for row in result["rows"]
    ]


async def test_the_loader_refuses_to_certify_a_database_with_unmapped_statuses(
    engine: Any, sale_with_an_unmapped_status: dict[str, Any]
) -> None:
    """Visible in a report is good; caught at load time is better.

    A deployment that adds a status word should learn about it from the loader,
    naming the word and the file to edit, rather than from a revenue number that
    quietly stopped adding up.
    """
    problems = await _loader_contract_problems(engine)

    assert any("en_proceso" in problem for problem in problems)


async def test_the_loader_certifies_the_untouched_demo_database(engine: Any) -> None:
    """The check must be silent when the mapping is complete.

    Without this, a check that always reported a problem would pass the test
    above and make the loader useless.
    """
    assert await _loader_contract_problems(engine) == []


async def _loader_contract_problems(engine: Any) -> list[str]:
    """Run the real loader's contract verification against *engine*."""
    import importlib.util

    path = _REPO_ROOT / "demo" / "load_demo_company.py"
    spec = importlib.util.spec_from_file_location("demo_loader_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with engine.connect() as conn:
        problems: list[str] = await module._verify_contract(conn)
    return problems


async def test_the_default_status_filter_excludes_cancelled_sales(
    engine: Any,
) -> None:
    """`default` must mean confirmed + pending, and the result must say so."""
    result = await _report(engine, "sales_by_zone", months_back=WHOLE_HISTORY, limit=50)

    counted = sum(row["sale_count"] for row in result["rows"])

    assert counted == SEEDED_CONFIRMED + SEEDED_PENDING
    assert result["meta"]["cancelled_included"] is False
    assert result["meta"]["statuses_included"] == ["confirmed", "pending"]


# --- The computed line total, the other thing a view can get wrong ----------


async def test_line_revenue_reconciles_with_invoice_header_revenue(
    engine: Any,
) -> None:
    """`agents_system_sale_items.amount` is computed; `agents_system_sales.amount` is not.

    The demo's invoice headers are derived from their lines, so the two must
    agree exactly. If the view's arithmetic were wrong — a missing quantity
    multiplier, say — every product report would disagree with every revenue
    report, and the agent would report both without noticing.
    """
    by_month = await _report(
        engine, "sales_by_month", months_back=WHOLE_HISTORY, limit=100
    )
    by_product = await _report(
        engine, "top_products", months_back=WHOLE_HISTORY, limit=500
    )

    header_revenue = sum(Decimalish(row["revenue"]) for row in by_month["rows"])
    line_revenue = sum(Decimalish(row["revenue"]) for row in by_product["rows"])

    assert header_revenue == line_revenue


def Decimalish(value: Any) -> Any:
    """`json_safe` turns NUMERIC into `str` on purpose (money must not become
    a float). Parse it back for arithmetic, still without floats."""
    from decimal import Decimal

    return Decimal(str(value))


# --- Grouping reports see the whole company --------------------------------


async def test_every_seeded_zone_and_segment_appears(engine: Any) -> None:
    by_zone = await _report(
        engine, "sales_by_zone", months_back=WHOLE_HISTORY, limit=50
    )
    by_segment = await _report(
        engine, "sales_by_segment", months_back=WHOLE_HISTORY, limit=50
    )

    assert len(by_zone["rows"]) == SEEDED_ZONES
    assert len(by_segment["rows"]) == SEEDED_SEGMENTS


async def test_top_customers_ranks_by_revenue_and_covers_the_padron(
    engine: Any,
) -> None:
    result = await _report(
        engine, "top_customers", months_back=WHOLE_HISTORY, limit=100
    )

    revenues = [Decimalish(row["revenue"]) for row in result["rows"]]

    assert len(result["rows"]) == SEEDED_CUSTOMERS
    assert revenues == sorted(revenues, reverse=True)


# --- top_products ranking (issue #44) ---------------------------------------
#
# The bug: `top_products` always ran revenue-ranked SQL, truncated to `limit`
# server-side, and a caller wanting a units ranking could only re-sort what
# already came back — never see a product LIMIT had already discarded. A
# fixture product makes that concrete: huge total_quantity (so it always
# leads a units ranking), negligible revenue (so it never reaches a
# revenue-ranked top-10). Cleaned up afterward so it cannot skew the fixed
# SEEDED_* totals or the header/line revenue reconciliation above.

_UNITS_FIXTURE_SKU = "ART-UNITS-FIXTURE"
_UNITS_FIXTURE_INVOICE = 900001
_UNITS_FIXTURE_QUANTITY = 1_000_000
_UNITS_FIXTURE_UNIT_PRICE = "0.01"


@pytest.fixture
async def high_units_low_revenue_product(engine: Any) -> Any:
    amount_expr = text(f"{_UNITS_FIXTURE_QUANTITY} * {_UNITS_FIXTURE_UNIT_PRICE}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO articulos (codigo_articulo, detalle, precio_lista) "
                "VALUES (:sku, :detalle, :precio)"
            ),
            {
                "sku": _UNITS_FIXTURE_SKU,
                "detalle": "issue #44 units-ranking regression fixture",
                "precio": _UNITS_FIXTURE_UNIT_PRICE,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO facturas "
                "(nro_factura, nro_cliente, fecha_emision, estado, importe_total) "
                "VALUES (:nro, 1, now(), 'facturada', " + str(amount_expr) + ")"
            ),
            {"nro": _UNITS_FIXTURE_INVOICE},
        )
        await conn.execute(
            text(
                "INSERT INTO factura_lineas "
                "(nro_factura, codigo_articulo, cantidad, precio_unitario) "
                "VALUES (:nro, :sku, :cantidad, :precio)"
            ),
            {
                "nro": _UNITS_FIXTURE_INVOICE,
                "sku": _UNITS_FIXTURE_SKU,
                "cantidad": _UNITS_FIXTURE_QUANTITY,
                "precio": _UNITS_FIXTURE_UNIT_PRICE,
            },
        )
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM factura_lineas WHERE nro_factura = :nro"),
                {"nro": _UNITS_FIXTURE_INVOICE},
            )
            await conn.execute(
                text("DELETE FROM facturas WHERE nro_factura = :nro"),
                {"nro": _UNITS_FIXTURE_INVOICE},
            )
            await conn.execute(
                text("DELETE FROM articulos WHERE codigo_articulo = :sku"),
                {"sku": _UNITS_FIXTURE_SKU},
            )


async def test_top_products_default_revenue_ranking_misses_the_high_unit_product(
    engine: Any, high_units_low_revenue_product: None
) -> None:
    """Reproduces the bug directly: the fixture's revenue (~$10k) sits far
    below the seeded catalogue's real per-product revenue over 360 invoices,
    so it never reaches a revenue-ranked top-10 — exactly the product a
    'top by units' question must not miss.
    """
    result = await _report(engine, "top_products", limit=10)

    skus = {row["sku"] for row in result["rows"]}
    assert _UNITS_FIXTURE_SKU not in skus


async def test_top_products_order_by_units_surfaces_the_high_unit_product(
    engine: Any, high_units_low_revenue_product: None
) -> None:
    """The fix: `order_by='units'` ranks (and truncates) by total_quantity
    in SQL, so the fixture's 1,000,000 units — dwarfing every seeded
    article's — puts it first, not merely present.
    """
    result = await _report(engine, "top_products", limit=10, order_by="units")

    assert result["rows"], "expected at least the fixture row"
    assert result["rows"][0]["sku"] == _UNITS_FIXTURE_SKU


async def test_top_products_ranks_by_revenue_when_order_by_is_unset(
    engine: Any,
) -> None:
    """Backward compatibility: omitting order_by must keep ranking by
    revenue, exactly like before this parameter existed."""
    result = await _report(engine, "top_products", months_back=WHOLE_HISTORY, limit=100)

    revenues = [Decimalish(row["revenue"]) for row in result["rows"]]

    assert revenues == sorted(revenues, reverse=True)


# --- Stock -----------------------------------------------------------------


async def test_low_stock_returns_only_products_at_or_under_the_reorder_point(
    engine: Any,
) -> None:
    result = await _report(engine, "low_stock", limit=100)

    assert result["rows"], "the seed puts several articles under the reorder point"
    for row in result["rows"]:
        assert row["on_hand"] <= row["reorder_point"]
        assert row["shortfall"] == row["reorder_point"] - row["on_hand"]


async def test_a_wider_threshold_returns_at_least_as_many_products(
    engine: Any,
) -> None:
    """`threshold_ratio` widens what counts as low; it must never narrow it."""
    tight = await _report(engine, "low_stock", threshold_ratio=1, limit=100)
    wide = await _report(engine, "low_stock", threshold_ratio=3, limit=100)

    assert wide["row_count"] >= tight["row_count"]
    assert wide["row_count"] <= SEEDED_ARTICLES


# --- Every report runs at all ----------------------------------------------


@pytest.mark.parametrize("report_name", sorted(CATALOG))
async def test_every_report_in_the_catalog_executes_against_the_demo(
    engine: Any, report_name: str
) -> None:
    """A report nobody runs is a report nobody knows is broken.

    Defaults only — this is the smoke check that the SQL is valid against a
    real contract-conforming database, separate from the figure assertions.
    """
    result = await run_report(engine, CATALOG[report_name], {})

    assert result["report"] == report_name
    assert isinstance(result["rows"], list)
    assert result["row_count"] == len(result["rows"])
