"""Unit tests for the generic run_report connector (D-023).

No real Postgres: agentsys.services.reports.run_report is monkeypatched
where needed, so these tests exercise only connector wiring - report-name
dispatch, error shaping, ToolSpec registration, and that `session` is never
forwarded to the report engine (AD-3: this tool uses its OWN dedicated
read-only engine, not the turn-scoped session).
"""
from __future__ import annotations

import asyncio
from typing import Any

from agentsys.connectors import badie_reports
from agentsys.connectors.report_connector import (
    build_report_connector,
    build_report_registry,
)


def _catalog() -> dict[str, Any]:
    return badie_reports.CATALOG


def test_connector_is_async_coroutine_function() -> None:
    connector = build_report_connector(object(), _catalog())
    assert asyncio.iscoroutinefunction(connector)


async def test_unknown_report_returns_error_dict_listing_valid_names() -> None:
    connector = build_report_connector(object(), _catalog())
    result = await connector({"report": "nope_not_real"}, session=object())

    assert "error" in result
    assert "nope_not_real" in result["error"]
    for name in _catalog():
        assert name in result["error"]


async def test_missing_report_key_returns_error_dict() -> None:
    connector = build_report_connector(object(), _catalog())
    result = await connector({})
    assert "error" in result


async def test_validation_error_from_bad_param_returns_error_dict() -> None:
    connector = build_report_connector(object(), _catalog())
    # months_back is bounded 1-24 on every report in the catalog.
    result = await connector({"report": "ventas_por_mes", "params": {"months_back": 999}})

    assert "error" in result
    assert "months_back" in result["error"]


async def test_session_kwarg_is_never_forwarded_to_run_report(monkeypatch: Any) -> None:
    from agentsys.connectors import report_connector

    captured: dict[str, Any] = {}

    async def fake_run_report(engine: Any, spec: Any, raw_params: Any) -> dict[str, Any]:
        captured["engine"] = engine
        captured["spec"] = spec
        captured["raw_params"] = raw_params
        return {"report": spec.name, "rows": [], "row_count": 0, "meta": {}}

    monkeypatch.setattr(report_connector, "run_report", fake_run_report)

    engine_sentinel = object()
    connector = build_report_connector(engine_sentinel, _catalog())
    session_sentinel = object()

    result = await connector(
        {"report": "resumen_estados", "params": {}}, session=session_sentinel
    )

    assert captured["engine"] is engine_sentinel
    assert result["report"] == "resumen_estados"
    assert "session" not in captured
    assert session_sentinel not in captured.values()


async def test_happy_path_returns_run_report_output_verbatim(monkeypatch: Any) -> None:
    from agentsys.connectors import report_connector

    canned = {
        "report": "top_clientes",
        "rows": [{"client_name": "x"}],
        "row_count": 1,
        "meta": {},
    }

    async def fake_run_report(engine: Any, spec: Any, raw_params: Any) -> dict[str, Any]:
        return canned

    monkeypatch.setattr(report_connector, "run_report", fake_run_report)

    connector = build_report_connector(object(), _catalog())
    result = await connector({"report": "top_clientes", "params": {}})

    assert result == canned


def test_input_schema_report_enum_matches_catalog_keys() -> None:
    registry = build_report_registry(object(), _catalog())
    spec = registry.get("run_report")
    enum_values = set(spec.input_schema["properties"]["report"]["enum"])
    assert enum_values == set(_catalog().keys())


def test_registry_registers_run_report_with_expected_permissions_and_revalidation() -> None:
    registry = build_report_registry(object(), _catalog())
    spec = registry.get("run_report")

    assert spec.required_permissions == ("read:reports",)
    assert spec.always_revalidate is True
    assert asyncio.iscoroutinefunction(spec.connector)


# ---------------------------------------------------------------------------
# D-023 wiring — the tool must be registrable into an EXISTING registry
# ---------------------------------------------------------------------------


def test_build_report_tool_spec_can_be_added_to_an_existing_registry() -> None:
    """`data-agent` needs four tools, only one of which is run_report.

    Handing back a fresh single-tool registry would force the caller to merge
    registries; the injector resolves every tool the role manifest names from
    ONE registry, so the spec has to be composable into the shared one.
    """
    from agentsys.connectors.report_connector import build_report_tool_spec
    from agentsys.harness.registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="already_here", required_permissions=(), connector=lambda i: {})
    )

    spec = build_report_tool_spec(object(), _catalog())
    registry.register(spec)

    assert "already_here" in registry
    assert "run_report" in registry
    assert spec.required_permissions == ("read:reports",)
    assert spec.always_revalidate is True
