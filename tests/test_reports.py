"""Unit tests for the generic report execution engine (D-023 / services.reports).

Strict TDD: written BEFORE agentsys.services.reports exists. Pure logic only
- no real AsyncEngine, no real Postgres. DB behavior against the real
bi_readonly-scoped connection is covered by tests/test_reports_integration.py
(@pytest.mark.integration).
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from agentsys.services.reports import (
    HARD_ROW_CEILING,
    ParamSpec,
    ReportSpec,
    ReportValidationError,
    build_bind_params,
    clamp_row_limit,
    fetch_rows,
    run_report,
    validate_params,
)


def _spec(**overrides: Any) -> ReportSpec:
    params = overrides.pop(
        "params",
        (
            ParamSpec(name="months_back", type=int, default=12, minimum=1, maximum=24),
            ParamSpec(name="limit", type=int, default=10, minimum=1, maximum=50),
            ParamSpec(name="status", type=str, default="all", allowed=("all", "confirmed")),
        ),
    )
    defaults: dict[str, Any] = dict(
        name="fake_report",
        description="A fake report for unit tests.",
        sql=text("SELECT 1 AS one LIMIT :limit"),
        params=params,
    )
    defaults.update(overrides)
    return ReportSpec(**defaults)


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_params_rejects_unknown_param() -> None:
    spec = _spec()
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {"bogus": 1})
    message = str(exc.value)
    assert "bogus" in message
    assert "months_back" in message  # names the valid options


def test_validate_params_applies_defaults_for_missing_keys() -> None:
    spec = _spec()
    validated = validate_params(spec, {})
    assert validated == {"months_back": 12, "limit": 10, "status": "all"}


def test_validate_params_rejects_wrong_type() -> None:
    spec = _spec()
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {"months_back": "twelve"})
    message = str(exc.value)
    assert "months_back" in message
    assert "int" in message


def test_validate_params_rejects_bool_for_int_param() -> None:
    """bool is an int subclass in Python - must not silently pass as int."""
    spec = _spec()
    with pytest.raises(ReportValidationError):
        validate_params(spec, {"months_back": True})


def test_validate_params_rejects_out_of_range_low() -> None:
    spec = _spec()
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {"months_back": 0})
    assert "months_back" in str(exc.value)


def test_validate_params_rejects_out_of_range_high() -> None:
    spec = _spec()
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {"months_back": 25})
    assert "months_back" in str(exc.value)


def test_validate_params_rejects_disallowed_value() -> None:
    spec = _spec()
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {"status": "cancelled"})
    message = str(exc.value)
    assert "status" in message
    assert "all" in message  # lists what IS valid


def test_validate_params_accepts_explicit_valid_override() -> None:
    spec = _spec()
    validated = validate_params(spec, {"months_back": 3, "limit": 5, "status": "confirmed"})
    assert validated == {"months_back": 3, "limit": 5, "status": "confirmed"}


def test_validate_params_missing_required_param_raises() -> None:
    spec = _spec(params=(ParamSpec(name="required_thing", type=int, required=True),))
    with pytest.raises(ReportValidationError) as exc:
        validate_params(spec, {})
    assert "required_thing" in str(exc.value)


# ---------------------------------------------------------------------------
# clamp_row_limit
# ---------------------------------------------------------------------------


def test_clamp_row_limit_passthrough_within_range() -> None:
    assert clamp_row_limit(42) == 42


def test_clamp_row_limit_caps_at_hard_ceiling() -> None:
    assert clamp_row_limit(10_000_000) == HARD_ROW_CEILING


def test_clamp_row_limit_floors_at_one() -> None:
    assert clamp_row_limit(-5) == 1
    assert clamp_row_limit(0) == 1


def test_clamp_row_limit_respects_narrower_ceiling_argument() -> None:
    assert clamp_row_limit(100, ceiling=20) == 20


# ---------------------------------------------------------------------------
# build_bind_params
# ---------------------------------------------------------------------------


def test_build_bind_params_applies_transform_and_bind_as() -> None:
    spec = _spec(
        params=(
            ParamSpec(
                name="status",
                type=str,
                default="all",
                bind_as="statuses",
                transform=lambda v: ("confirmed", "pending") if v == "all" else (v,),
            ),
        )
    )
    validated = validate_params(spec, {})
    bind_params = build_bind_params(spec, validated)
    assert bind_params == {"statuses": ("confirmed", "pending")}


def test_build_bind_params_defaults_bind_name_to_param_name() -> None:
    spec = _spec(params=(ParamSpec(name="limit", type=int, default=10),))
    validated = validate_params(spec, {})
    bind_params = build_bind_params(spec, validated)
    assert bind_params == {"limit": 10}


# ---------------------------------------------------------------------------
# fetch_rows
# ---------------------------------------------------------------------------


class _FakeRow:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(_FakeRow(r) for r in self._rows)


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed_with: tuple[Any, Any] | None = None

    async def execute(self, stmt: Any, params: Any) -> _FakeResult:
        self.executed_with = (stmt, params)
        return _FakeResult(self._rows)

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeEngine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.connection = _FakeConnection(rows)

    def connect(self) -> _FakeConnection:
        return self.connection


async def test_fetch_rows_executes_sql_with_bind_params_and_maps_rows() -> None:
    spec = _spec()
    engine: Any = _FakeEngine([{"one": 1}, {"one": 2}])
    rows = await fetch_rows(engine, spec, {"limit": 10})

    assert rows == [{"one": 1}, {"one": 2}]
    assert engine.connection.executed_with is not None
    _, bound = engine.connection.executed_with
    assert bound == {"limit": 10}


# ---------------------------------------------------------------------------
# run_report orchestration
# ---------------------------------------------------------------------------


async def test_run_report_clamps_limit_and_attaches_metadata(monkeypatch: Any) -> None:
    # No declared `maximum` on `limit` here on purpose: this test proves the
    # AD-5 hard ceiling in run_report applies even when a report's own
    # ParamSpec bound wouldn't have caught an absurd value.
    spec = _spec(
        params=(
            ParamSpec(name="limit", type=int, default=10),
            ParamSpec(name="status", type=str, default="all"),
        ),
        filter_metadata=lambda validated: {"statuses_included": [validated["status"]]},
    )

    captured: dict[str, Any] = {}

    async def fake_fetch_rows(
        engine: Any, spec_arg: Any, bind_params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        captured["bind_params"] = bind_params
        return [{"one": 1}]

    monkeypatch.setattr("agentsys.services.reports.fetch_rows", fake_fetch_rows)

    result = await run_report(object(), spec, {"limit": 10_000_000})

    assert result["report"] == "fake_report"
    assert result["rows"] == [{"one": 1}]
    assert result["row_count"] == 1
    assert captured["bind_params"]["limit"] == HARD_ROW_CEILING
    assert result["meta"] == {"statuses_included": ["all"]}


async def test_run_report_propagates_validation_error_before_touching_engine() -> None:
    spec = _spec()

    class _ExplodingEngine:
        def connect(self) -> Any:
            raise AssertionError("engine must not be touched when validation fails")

    with pytest.raises(ReportValidationError):
        await run_report(_ExplodingEngine(), spec, {"months_back": 999})


# ---------------------------------------------------------------------------
# D-023 — tool output must survive plain JSON serialization
# ---------------------------------------------------------------------------


def test_json_safe_converts_decimal_and_datetime_exactly() -> None:
    """Postgres NUMERIC arrives as `Decimal`, which `json.dumps` refuses.

    Caught end-to-end, not in unit tests: every direct call here had been
    printing with `default=str`, which silently papered over it. Through the
    real agent the tool result is serialized without that fallback and the
    request died with HTTP 500.

    Money converts to `str`, not `float`: `float` would introduce binary
    representation artifacts in figures a human is going to reconcile against
    the database, and the agent has no reason to do arithmetic on them.
    """
    import json
    from datetime import UTC, datetime
    from decimal import Decimal

    from agentsys.services.reports import json_safe

    row = {
        "revenue": Decimal("5014100.00"),
        "when": datetime(2026, 7, 28, 3, 47, tzinfo=UTC),
        "count": 31,
        "zone": "Morón",
        "nothing": None,
    }
    safe = json_safe(row)

    assert safe["revenue"] == "5014100.00"
    assert safe["count"] == 31
    assert safe["zone"] == "Morón"
    assert safe["nothing"] is None
    json.dumps(safe)  # must not raise


def test_json_safe_handles_nested_rows() -> None:
    import json
    from decimal import Decimal

    from agentsys.services.reports import json_safe

    json.dumps(json_safe([{"a": Decimal("1.5")}, {"a": Decimal("2.5")}]))
