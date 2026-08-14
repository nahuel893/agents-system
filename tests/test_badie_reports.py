"""Unit tests for the BADIE report catalog (D-023).

Catalog integrity and pure business-filter logic only - no real Postgres
connection. Live execution against the seeded DB is covered by
tests/test_reports_integration.py (@pytest.mark.integration).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentsys.connectors import badie_reports as br
from agentsys.services.reports import (
    HARD_ROW_CEILING,
    ReportSpec,
    build_bind_params,
    validate_params,
)

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
    meta_all = spec.filter_metadata(validated_all, build_bind_params(spec, validated_all))
    assert set(meta_all["statuses_included"]) == {"confirmed", "pending", "cancelled"}
    assert meta_all["cancelled_included"] is True

    validated_default = validate_params(spec, {})
    meta_default = spec.filter_metadata(
        validated_default, build_bind_params(spec, validated_default)
    )
    assert "cancelled" not in meta_default["statuses_included"]
    assert meta_default["cancelled_included"] is False


def test_resumen_estados_metadata_always_reports_all_statuses() -> None:
    spec = br.CATALOG["resumen_estados"]
    validated = validate_params(spec, {})
    meta = spec.filter_metadata(validated, build_bind_params(spec, validated))
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


# ---------------------------------------------------------------------------
# D-023 follow-up — the time window must be disclosed, not just the status
# ---------------------------------------------------------------------------


def test_every_windowed_report_discloses_its_time_window() -> None:
    """`months_back` is a 30-day multiple, NOT a calendar month.

    A caller asking for "the last 12 months" silently gets 360 days. That
    approximation is defensible; leaving it undisclosed is not. An analytics
    answer that does not state the period it covers cannot be checked, and a
    number nobody can check is indistinguishable from a wrong one.
    """
    for name, spec in br.CATALOG.items():
        if "months_back" not in spec.param_names():
            continue
        validated = validate_params(spec, {})
        meta = spec.filter_metadata(validated, build_bind_params(spec, validated))

        assert "window_days" in meta, f"{name} does not disclose its window length"
        assert "window_start" in meta, f"{name} does not disclose its window start"
        assert meta["window_days"] == br._DAYS_PER_MONTH * validated["months_back"]
        assert meta["window_is_calendar_months"] is False


def test_window_metadata_tracks_the_requested_months_back() -> None:
    spec = br.CATALOG["ventas_por_zona"]
    validated = validate_params(spec, {"months_back": 3})
    meta = spec.filter_metadata(validated, build_bind_params(spec, validated))
    assert meta["window_days"] == 90


# ---------------------------------------------------------------------------
# The disclosed window must be DERIVED from the query, never recomputed
#
# `_months_back_to_since` reads the clock. It ran twice per report: once in
# build_bind_params to produce the `since` actually bound into the SQL, and
# again in _window_metadata AFTER fetch_rows returned. The disclosed window
# was therefore a second, later clock read describing a query it never saw.
#
# _window_metadata exists specifically so the figure would be checkable. A
# derived disclosure cannot be wrong; a recomputed one can.
# ---------------------------------------------------------------------------


async def test_window_start_is_derived_from_the_bound_since(monkeypatch: Any) -> None:
    """The disclosed `window_start` must equal the bound `since`, exactly.

    The clock stub returns a DIFFERENT instant on every call, so "derived"
    and "recomputed" give different answers deterministically — comparing two
    real `datetime.now()` reads would differ only by microseconds and make
    this test a coin flip.
    """
    from agentsys.services.reports import run_report

    reads = 0
    base = datetime(2026, 1, 1, tzinfo=UTC)

    def drifting_utcnow() -> datetime:
        nonlocal reads
        reads += 1
        # Each successive read answers one hour later than the previous one.
        return base + timedelta(hours=reads)

    monkeypatch.setattr(br, "_utcnow", drifting_utcnow)

    spec = br.CATALOG["ventas_por_zona"]
    captured: dict[str, Any] = {}

    async def fake_fetch_rows(
        engine: Any, spec_arg: Any, bind_params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        captured["bind_params"] = dict(bind_params)
        return []

    monkeypatch.setattr("agentsys.services.reports.fetch_rows", fake_fetch_rows)

    result = await run_report(object(), spec, {"months_back": 6})

    assert result["meta"]["window_start"] == captured["bind_params"]["since"].isoformat()


async def test_window_disclosure_reads_the_clock_once_per_report(
    monkeypatch: Any,
) -> None:
    """One report run, one clock read.

    Pins the *cause*, not only the symptom: as long as the window is computed
    twice, a later edit can let the two drift apart again.

    Patches `_utcnow`, NOT `_months_back_to_since`. `ParamSpec.transform`
    captured a direct reference to `_months_back_to_since` at import time, so
    a monkeypatched module global is invisible to the bind path — an earlier
    draft of this test patched the helper, counted only the disclosure call,
    and passed against the two-clock-read bug it was written to catch.
    """
    from agentsys.services.reports import run_report

    reads = 0

    def counting_utcnow() -> datetime:
        nonlocal reads
        reads += 1
        return datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(br, "_utcnow", counting_utcnow)

    async def fake_fetch_rows(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("agentsys.services.reports.fetch_rows", fake_fetch_rows)

    await run_report(object(), br.CATALOG["ventas_por_zona"], {"months_back": 6})

    assert reads == 1
