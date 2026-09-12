"""The portable sales report catalog may only touch the contract views.

Every company's sales tables are similar but not identical, so the platform
cannot ship SQL against any one company's schema — that is what
`connectors/acme_reports.py` is, and why it is not reusable.

The catalog here reads from a fixed set of VIEWS instead. A deployment creates
those views over whatever its own tables are called; the reports never change.
These tests are what keeps that promise honest: the moment a report reaches for
a real table name, it stops being portable, and the failure has to be loud.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentsys.connectors.sales_reports import (
    ALL_STATUSES,
    CATALOG,
    CONTRACT_VIEWS,
    UNMAPPED_STATUS,
)

_DEMO_VIEWS_SQL = (
    Path(__file__).resolve().parents[1] / "demo" / "company" / "02_views.sql"
)

#: Written out independently of the module under test. Deriving it from
#: CONTRACT_VIEWS would make the test compare the module to itself, which
#: cannot fail for any implementation.
EXPECTED_VIEWS = {
    "agentsys_sales",
    "agentsys_sale_items",
    "agentsys_customers",
    "agentsys_stock",
}

EXPECTED_REPORTS = {
    "sales_by_month",
    "top_customers",
    "sales_by_zone",
    "sales_by_segment",
    "top_products",
    "status_summary",
    "low_stock",
}

#: Anything appearing after FROM or JOIN. A report is portable only if every
#: one of these is a contract view.
_RELATION_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_.]*)", re.IGNORECASE)


def _relations(sql_text: str) -> set[str]:
    return {match.group(1).lower() for match in _RELATION_RE.finditer(sql_text)}


def test_the_contract_names_exactly_the_four_expected_views() -> None:
    assert set(CONTRACT_VIEWS) == EXPECTED_VIEWS


# --- The status vocabulary, and what a deployment does with a word it lacks ---


def test_the_reserved_unmapped_status_is_not_one_of_the_canonical_three() -> None:
    """The whole point is that it cannot be mistaken for a real status.

    If `UNMAPPED_STATUS` were 'cancelled', every filtered report would exclude
    unmapped rows (correct) while `status_summary` reported them as
    cancellations (invented). A value outside the vocabulary cannot do that.
    """
    assert UNMAPPED_STATUS not in ALL_STATUSES


def test_the_demo_maps_unrecognized_source_statuses_to_the_reserved_word() -> None:
    """The demo is the worked example deployments copy, so its CASE matters.

    It used to end `ELSE 'cancelled'`, reasoning that excluding unknown rows
    from revenue is the conservative choice. It is conservative for revenue and
    a fabrication for `status_summary`, which does not filter by status and
    would therefore report a cancellation count that no row in the source
    supports. Both halves have to be honest, so the ELSE must land outside the
    vocabulary.
    """
    sql = _DEMO_VIEWS_SQL.read_text()

    else_targets = {
        match.group(1).lower()
        for match in re.finditer(r"\bELSE\s+'([a-z_]+)'", sql, re.IGNORECASE)
    }

    assert else_targets, "no CASE ... ELSE found; this test would pass vacuously"
    assert else_targets == {UNMAPPED_STATUS}
    assert not else_targets & set(ALL_STATUSES)


def test_the_catalog_offers_exactly_the_expected_reports() -> None:
    assert set(CATALOG) == EXPECTED_REPORTS


@pytest.mark.parametrize("report_name", sorted(EXPECTED_REPORTS))
def test_every_report_reads_only_from_contract_views(report_name: str) -> None:
    """A report that names a real table has silently become client-specific.

    This is the whole portability guarantee, so it is asserted per report
    rather than over the catalog as a whole — a failure names the offender.
    """
    sql_text = str(CATALOG[report_name].sql)

    foreign = _relations(sql_text) - set(CONTRACT_VIEWS)

    assert foreign == set(), (
        f"report '{report_name}' reads from {sorted(foreign)}, which is not a "
        f"contract view — it is no longer portable across deployments"
    )


def test_the_relation_check_would_actually_catch_a_client_table() -> None:
    """The guard above is regex-based, so prove the regex sees a violation.

    Without this, a broken pattern would make every portability assertion pass
    vacuously — the tests would look green and guarantee nothing.
    """
    offending = "SELECT 1 FROM orders o JOIN clients c ON c.id = o.client_id"

    assert _relations(offending) == {"orders", "clients"}
    assert _relations(offending) - set(CONTRACT_VIEWS) == {"orders", "clients"}


@pytest.mark.parametrize("report_name", sorted(EXPECTED_REPORTS))
def test_every_report_binds_its_values_instead_of_interpolating_them(
    report_name: str,
) -> None:
    """Every declared parameter must reach the SQL as a named bindparam.

    The catalog is static SQL by design (AD-2). A parameter that does not
    appear as `:name` is either dead or — worse — being spliced in somewhere,
    which reintroduces exactly the injection surface the bindparam rule exists
    to remove.

    Quoted literals are NOT banned: `date_trunc('month', ...)` is static SQL,
    and a blanket ban would only push reports into worse shapes.
    """
    spec = CATALOG[report_name]
    sql_text = str(spec.sql)

    assert "%s" not in sql_text
    assert "{" not in sql_text

    for param in spec.params:
        bind = param.bind_name()
        # An expanding bindparam (the variable-length IN list) does not render
        # as `:name`; SQLAlchemy defers it and prints `[POSTCOMPILE_name]`.
        # Both forms are bound values — only the rendering differs.
        bound = f":{bind}" in sql_text or f"[POSTCOMPILE_{bind}]" in sql_text
        assert bound, (
            f"report '{report_name}' declares parameter '{param.name}' "
            f"(bound as '{bind}') but its SQL never binds it"
        )


@pytest.mark.parametrize("report_name", sorted(EXPECTED_REPORTS))
def test_every_report_is_bounded_by_a_row_limit(report_name: str) -> None:
    """An unbounded report can return the whole table into a chat message."""
    spec = CATALOG[report_name]

    assert spec.limit_param == "limit"
    assert "limit" in spec.param_names()
    assert ":limit" in str(spec.sql)


@pytest.mark.parametrize("report_name", sorted(EXPECTED_REPORTS))
def test_every_report_has_a_description_a_model_can_act_on(
    report_name: str,
) -> None:
    """The description is the only thing the model sees when choosing."""
    description = CATALOG[report_name].description

    assert len(description) > 40, f"report '{report_name}' is underdescribed"
    assert description.strip().endswith(".")
