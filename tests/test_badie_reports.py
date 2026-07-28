"""Unit tests for the BADIE report catalog (D-023).

Catalog integrity and pure business-filter logic only - no real Postgres
connection. Live execution against the seeded DB is covered by
tests/test_reports_integration.py (@pytest.mark.integration).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentsys.connectors import badie_reports as br
from agentsys.services.reports import HARD_ROW_CEILING, ReportSpec, validate_params

EXPECTED_REPORT_NAMES = {
    "ventas_por_mes",
    "top_clientes",
    "ventas_por_zona",
    "ventas_por_tipo_negocio",
    "top_productos",
    "resumen_estados",
}


def test_catalog_has_exactly_the_six_expected_reports() -> None:
    assert set(br.CATALOG.keys()) == EXPECTED_REPORT_NAMES


def test_every_catalog_entry_key_matches_its_spec_name() -> None:
    for key, spec in br.CATALOG.items():
        assert isinstance(spec, ReportSpec)
        assert spec.name == key


def test_every_report_has_a_limit_param_with_a_positive_minimum() -> None:
    for name, spec in br.CATALOG.items():
        limit_params = [p for p in spec.params if p.name == "limit"]
        assert limit_params, f"{name} is missing a 'limit' param"
        assert limit_params[0].minimum == 1


def test_no_report_declares_a_limit_maximum_the_hard_ceiling_clamp_handles_it() -> None:
    """AD-5: an over-large `limit` must degrade gracefully to the hard
    ceiling in run_report, not bounce back a validation error - so no
    report here declares its own `maximum` on `limit`."""
    for name, spec in br.CATALOG.items():
        limit_params = [p for p in spec.params if p.name == "limit"]
        assert limit_params[0].maximum is None, (
            f"{name} declares a 'limit' maximum, which would make "
            "validate_params reject an over-large request instead of "
            "letting the hard ceiling clamp it."
        )


async def test_a_huge_limit_against_a_real_catalog_spec_is_clamped_not_rejected(
    monkeypatch: Any,
) -> None:
    from agentsys.services import reports as reports_module

    captured: dict[str, Any] = {}

    async def fake_fetch_rows(
        engine: Any, spec: Any, bind_params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        captured["bind_params"] = bind_params
        return []

    monkeypatch.setattr(reports_module, "fetch_rows", fake_fetch_rows)

    spec = br.CATALOG["top_productos"]
    result = await reports_module.run_report(object(), spec, {"limit": 999_999})

    assert result["row_count"] == 0
    assert captured["bind_params"]["limit"] == HARD_ROW_CEILING


def test_every_report_has_months_back_bounded_between_one_and_twenty_four() -> None:
    for name, spec in br.CATALOG.items():
        months_back = [p for p in spec.params if p.name == "months_back"]
        assert months_back, f"{name} is missing 'months_back'"
        assert months_back[0].minimum == 1
        assert months_back[0].maximum == 24


def test_five_reports_expose_a_status_filter_resumen_estados_does_not() -> None:
    for name, spec in br.CATALOG.items():
        param_names = spec.param_names()
        if name == "resumen_estados":
            assert "status" not in param_names
        else:
            assert "status" in param_names, f"{name} is missing a 'status' filter"


def test_resolve_statuses_default_excludes_cancelled() -> None:
    assert "cancelled" not in br._resolve_statuses("default")
    assert set(br._resolve_statuses("default")) == {"confirmed", "pending"}


def test_resolve_statuses_all_includes_cancelled() -> None:
    assert set(br._resolve_statuses("all")) == {"confirmed", "pending", "cancelled"}


def test_resolve_statuses_specific_value_is_singleton() -> None:
    assert br._resolve_statuses("cancelled") == ("cancelled",)
    assert br._resolve_statuses("confirmed") == ("confirmed",)
    assert br._resolve_statuses("pending") == ("pending",)


def test_status_filter_metadata_states_whether_cancelled_is_included() -> None:
    spec = br.CATALOG["top_clientes"]

    validated_all = validate_params(spec, {"status": "all"})
    meta_all = spec.filter_metadata(validated_all)
    assert set(meta_all["statuses_included"]) == {"confirmed", "pending", "cancelled"}
    assert meta_all["cancelled_included"] is True

    validated_default = validate_params(spec, {})
    meta_default = spec.filter_metadata(validated_default)
    assert "cancelled" not in meta_default["statuses_included"]
    assert meta_default["cancelled_included"] is False


def test_resumen_estados_metadata_always_reports_all_statuses() -> None:
    spec = br.CATALOG["resumen_estados"]
    validated = validate_params(spec, {})
    meta = spec.filter_metadata(validated)
    assert set(meta["statuses_included"]) == {"confirmed", "pending", "cancelled"}
    assert meta["cancelled_included"] is True


def test_months_back_to_since_is_thirty_days_per_month_before_now() -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    since = br._months_back_to_since(6, now=now)
    assert since == now - timedelta(days=180)


def test_no_report_sql_contains_percent_s_or_curly_brace_placeholders() -> None:
    """Guards AD-2: SQL must never carry leftover %s / {} interpolation
    markers - every value must be a named SQLAlchemy bindparam."""
    for spec in br.CATALOG.values():
        raw_sql = spec.sql.text
        assert "%s" not in raw_sql
        assert "{" not in raw_sql
        assert "}" not in raw_sql


def test_every_report_sql_has_a_limit_bindparam() -> None:
    for name, spec in br.CATALOG.items():
        assert ":limit" in spec.sql.text, f"{name} SQL is missing a :limit bind"
