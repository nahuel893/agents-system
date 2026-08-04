"""Unit tests for the generic run_report connector (D-023).

No real Postgres: agentsys.services.reports.run_report is monkeypatched
where needed, so these tests exercise only connector wiring - report-name
dispatch, error shaping, ToolSpec registration, and that `session` is never
forwarded to the report engine (AD-3: this tool uses its OWN dedicated
read-only engine, not the turn-scoped session).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

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


# ---------------------------------------------------------------------------
# A database failure must reach the agent as a RESULT, not as an exception
#
# `fetch_rows` runs a bare `await conn.execute`. An OperationalError
# (connection refused, statement_timeout, lock timeout) is never a
# TimeoutError, so `_execute_tools` — which catches TimeoutError and
# PolicyViolation — does not see it, and neither does run_turn. It escapes
# to whichever HTTP entry point is in play, and the two degrade differently:
# WhatsApp's broad `except Exception` returns 200 to Meta and the customer
# gets NO reply at all, while the OpenAI adapter has no try/except and
# returns a raw 500 that never reaches structlog.
#
# The tool boundary is the right place to stop this: the agent can say
# "the reporting database is unavailable", which is a real answer.
# ---------------------------------------------------------------------------


async def test_connector_returns_a_structured_error_when_the_database_fails(
    monkeypatch: Any,
) -> None:
    from sqlalchemy.exc import OperationalError

    from agentsys.connectors import report_connector as rc

    async def exploding_run_report(*_: Any, **__: Any) -> dict[str, Any]:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(rc, "run_report", exploding_run_report)

    connector = rc.build_report_connector(object(), _catalog())
    result = await connector({"report": "ventas_por_zona", "params": {}})

    assert "error" in result
    assert result["error_kind"] == "database_unavailable"


async def test_connector_database_error_never_leaks_the_connection_string(
    monkeypatch: Any,
) -> None:
    """The DSN is in scope at the failure site and must not reach the model.

    SQLAlchemy's OperationalError stringifies the statement and the driver's
    own message, and a driver message routinely carries host, user and — for
    a URL-style DSN — the password. That string would land in the agent's
    message history, get summarized, and be echoed to a customer over
    WhatsApp. The tool returns a FIXED message; the real exception goes to
    the log.
    """
    from sqlalchemy.exc import OperationalError

    from agentsys.connectors import report_connector as rc

    secret = "sup3rs3cr3t-bi-password"

    async def exploding_run_report(*_: Any, **__: Any) -> dict[str, Any]:
        raise OperationalError(
            "SELECT 1",
            {},
            Exception(
                f"could not connect to postgresql://bi_readonly:{secret}@db:5432/badie"
            ),
        )

    monkeypatch.setattr(rc, "run_report", exploding_run_report)

    connector = rc.build_report_connector(object(), _catalog())
    result = await connector({"report": "ventas_por_zona", "params": {}})

    serialized = json.dumps(result)
    assert secret not in serialized
    assert "bi_readonly" not in serialized
    assert "db:5432" not in serialized


async def test_connector_marks_an_empty_result_as_a_successful_no_match(
    monkeypatch: Any,
) -> None:
    """Zero rows and a broken pipeline must not look the same to the model.

    Without a positive marker, `rows: []` is exactly what a silently failing
    query returns, and the deployment's own policy.md says a confident zero
    is worse than an error. The flag lets the agent say "the query ran and
    matched nothing" instead of guessing.
    """
    from agentsys.connectors import report_connector as rc

    async def empty_run_report(*_: Any, **__: Any) -> dict[str, Any]:
        return {"report": "ventas_por_zona", "rows": [], "row_count": 0, "meta": {}}

    monkeypatch.setattr(rc, "run_report", empty_run_report)

    connector = rc.build_report_connector(object(), _catalog())
    result = await connector({"report": "ventas_por_zona", "params": {}})

    assert result["row_count"] == 0
    assert result["empty_result"] is True
    assert "error" not in result


async def test_connector_does_not_mark_a_non_empty_result_as_empty(
    monkeypatch: Any,
) -> None:
    """Negative control for the flag above — it must track the row count."""
    from agentsys.connectors import report_connector as rc

    async def one_row(*_: Any, **__: Any) -> dict[str, Any]:
        return {"report": "ventas_por_zona", "rows": [{"a": 1}], "row_count": 1, "meta": {}}

    monkeypatch.setattr(rc, "run_report", one_row)

    connector = rc.build_report_connector(object(), _catalog())
    result = await connector({"report": "ventas_por_zona", "params": {}})

    assert result.get("empty_result") is False


# ---------------------------------------------------------------------------
# The OpenAI adapter must not turn a turn failure into a silent 500
#
# The other half of the same review finding. The connector above stops
# SQLAlchemy errors at the tool boundary, but ANY other exception escaping
# run_turn hit `openai_adapter` with no try/except at all: FastAPI returned a
# raw 500, and RequestIdMiddleware is try/finally with no `except`, so nothing
# reached structlog. There is no Sentry and no metrics in this codebase, so an
# unlogged 500 is an outage nobody can see.
# ---------------------------------------------------------------------------


async def test_adapter_logs_and_reports_a_failed_turn(monkeypatch: Any) -> None:
    from fastapi import HTTPException

    from agentsys.integration import openai_adapter as oa

    class _BoomRuntime:
        async def run_turn(self, **_: Any) -> Any:
            raise RuntimeError("connector blew up")

    fake_logger = MagicMock()
    monkeypatch.setattr(oa.structlog, "get_logger", lambda *a, **k: fake_logger)

    from unittest.mock import AsyncMock

    request = MagicMock()
    request.app.state.runtimes = {"badie__sales-agent": _BoomRuntime()}
    request.json = AsyncMock(
        return_value={
            "model": "badie__sales-agent",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    with pytest.raises(HTTPException) as excinfo:
        await oa.chat_completions(request)

    assert excinfo.value.status_code == 500
    assert fake_logger.exception.called
    # The client is told something went wrong, not what — an internal
    # exception message can carry a DSN, a prompt, or a customer's data.
    assert "connector blew up" not in str(excinfo.value.detail)
