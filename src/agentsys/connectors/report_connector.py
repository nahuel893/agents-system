"""Async BI report connector (D-023).

Wraps `services.reports.run_report` into a harness connector following the
D-009 async contract: `async def connector(inputs, *, session=None) -> dict`.

IMPORTANT (AD-3): this tool executes against its OWN dedicated read-only
engine - the `bi_readonly`-scoped Postgres role (see
`architecture/bi-readonly-db-role`) - never the app's read-write engine.
The `session` kwarg the D-009 contract normally injects is the
turn-scoped, read-write SQLAlchemy AsyncSession; forwarding it here would
run analytical report queries under the app's write-capable role and on
the same connection pool used for the sales conversation path, defeating
the entire point of the dedicated read-only role as the guardrail that
holds even if every layer above it has a bug. So `session` is accepted
(to satisfy the connector contract) and then deliberately IGNORED.

Connectors MUST NOT commit or rollback - `run_report` only ever executes a
SELECT and never calls either.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.harness.registry import ToolRegistry, ToolSpec
from agentsys.services.reports import ReportSpec, ReportValidationError, run_report

_logger = structlog.get_logger(__name__)

_NOT_CONFIGURED_MESSAGE = (
    "Reporting is not available on this deployment — no read-only reporting "
    "database is bound — so no report can be run. Say so plainly: do not "
    "estimate, and do not present any number as if a report had returned."
)
"""What the tool answers when no BI engine is bound.

`platform/roles/data-agent/manifest.md` names `run_report`, and a manifest is
a promise about what the platform CAN equip: registering the tool only inside
the app's lifespan, behind an env var, makes `resolve_tool_surface` raise
`InjectionError` everywhere else and leaves the role not partially usable but
entirely unbuildable. The tool therefore always exists; whether a database
sits behind it is a runtime fact it reports, not an import-time landmine.

Deliberately names no environment variable. This text reaches the model and
from there a customer, so it is the wrong place for operator configuration
detail — and it is also the fail-closed path when the role turns out to be
writable, where "the variable is unset" would simply be false. Operators
get the specific reason in the startup log.
"""

_DB_UNAVAILABLE_MESSAGE = (
    "The reporting database is unavailable right now, so this report could "
    "not be run. Say so plainly — do not estimate, and do not present any "
    "number as if the report had returned."
)
"""Fixed text, deliberately.

Never interpolate the caught exception. `SQLAlchemyError` stringifies the
statement plus the driver's own message, and a driver message routinely
carries host, user and — for a URL-style DSN — the password. That string
would land in the agent's message history, get summarized, and be echoed to
a customer over WhatsApp. The real exception goes to the log instead.
"""

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]

_RUN_REPORT_DESCRIPTION = (
    "Run one of the pre-approved analytical reports over sales data. You "
    "select a report by name from a closed list and supply that report's "
    "parameters - you never write or compose SQL. Every result's "
    "`meta.statuses_included` (and `meta.cancelled_included`) states "
    "exactly which order statuses were counted; never state a revenue or "
    "order-count number without checking that field first."
)


def _input_schema(catalog: dict[str, ReportSpec]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "report": {
                "type": "string",
                "enum": sorted(catalog.keys()),
                "description": "Name of the report to run.",
            },
            "params": {
                "type": "object",
                "description": (
                    "Report-specific parameters. See the chosen report's "
                    "own description for accepted keys - an unrecognized "
                    "key is rejected, not ignored."
                ),
            },
        },
        "required": ["report"],
    }


def build_report_connector(
    engine: AsyncEngine | None, catalog: dict[str, ReportSpec]
) -> AsyncConnector:
    """Build the async `run_report` connector closure over *engine*/*catalog*.

    *engine* must be a DEDICATED read-only engine (the `bi_readonly` role,
    AD-3) supplied by the caller - this function does not construct one.
    """

    async def run_report_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if engine is None:
            # Registered but unbound. Falling through would reach
            # `engine.connect()` as an AttributeError — not a
            # SQLAlchemyError, so it would escape the handler below and
            # land back at the uncaught-exception behaviour that handler
            # exists to remove.
            return {
                "error": _NOT_CONFIGURED_MESSAGE,
                "error_kind": "bi_not_configured",
            }

        report_name = inputs.get("report")
        if report_name not in catalog:
            valid = ", ".join(sorted(catalog.keys()))
            return {
                "error": (
                    f"Unknown report '{report_name}'. Valid reports: {valid}."
                )
            }

        spec = catalog[report_name]
        raw_params = inputs.get("params") or {}
        try:
            result = await run_report(engine, spec, raw_params)
        except ReportValidationError as error:
            return {"error": str(error)}
        except SQLAlchemyError:
            # A DB failure has to come back as a RESULT, not an exception.
            # `fetch_rows` runs a bare `await conn.execute`, and an
            # OperationalError (connection refused, statement_timeout, lock
            # timeout) is never a TimeoutError — so `_execute_tools`, which
            # catches TimeoutError and PolicyViolation, does not see it, and
            # neither does run_turn. It escaped to whichever HTTP entry point
            # was in play, and the two degraded differently: WhatsApp's broad
            # `except Exception` returned 200 to Meta and the customer got no
            # reply at all, while the OpenAI adapter had no handler and
            # returned a raw 500 that never reached structlog.
            #
            # The tool boundary is the right place to stop it, because "the
            # reporting database is unavailable" is a real answer the agent
            # can give.
            _logger.exception(
                "bi.report_failed", report=report_name, params=sorted(raw_params)
            )
            return {
                "error": _DB_UNAVAILABLE_MESSAGE,
                "error_kind": "database_unavailable",
            }

        # Zero rows and a broken pipeline must not look the same to the model:
        # `rows: []` is exactly what a silently failing query returns, and the
        # deployment's policy.md holds that a confident zero is worse than an
        # error. The flag lets the agent distinguish "ran, matched nothing"
        # from "did not run".
        result["empty_result"] = result.get("row_count", 0) == 0
        return result

    return run_report_connector


def build_report_tool_spec(
    engine: AsyncEngine | None, catalog: dict[str, ReportSpec]
) -> ToolSpec:
    """Return the `run_report` ToolSpec over *catalog*, unregistered.

    Handed back loose rather than pre-wrapped in a registry because the
    injector resolves every tool a role manifest names from ONE registry:
    `data-agent` asks for four tools and this is only one of them. A caller
    with a populated registry composes this in; a caller with none uses
    `build_report_registry` below.

    `required_permissions=("read:reports",)` reuses the permission the
    `data-agent` role already grants (no new permission needed).
    `always_revalidate=True` (AD-4) closes the Layer-2 interceptor gap that
    would otherwise apply only to `write:`/`send:` tools - this is a
    sensitive read.
    """
    return ToolSpec(
        name="run_report",
        description=_RUN_REPORT_DESCRIPTION,
        required_permissions=("read:reports",),
        input_schema=_input_schema(catalog),
        connector=build_report_connector(engine, catalog),
        always_revalidate=True,
    )


def build_report_registry(
    engine: AsyncEngine | None, catalog: dict[str, ReportSpec]
) -> ToolRegistry:
    """Return a ToolRegistry holding only the `run_report` tool over *catalog*."""
    registry = ToolRegistry()
    registry.register(build_report_tool_spec(engine, catalog))
    return registry
