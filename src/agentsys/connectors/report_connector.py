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

from sqlalchemy.ext.asyncio import AsyncEngine

from agentsys.harness.registry import ToolRegistry, ToolSpec
from agentsys.services.reports import ReportSpec, ReportValidationError, run_report

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
    engine: AsyncEngine, catalog: dict[str, ReportSpec]
) -> AsyncConnector:
    """Build the async `run_report` connector closure over *engine*/*catalog*.

    *engine* must be a DEDICATED read-only engine (the `bi_readonly` role,
    AD-3) supplied by the caller - this function does not construct one.
    """

    async def run_report_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
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
            return await run_report(engine, spec, raw_params)
        except ReportValidationError as error:
            return {"error": str(error)}

    return run_report_connector


def build_report_tool_spec(
    engine: AsyncEngine, catalog: dict[str, ReportSpec]
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
    engine: AsyncEngine, catalog: dict[str, ReportSpec]
) -> ToolRegistry:
    """Return a ToolRegistry holding only the `run_report` tool over *catalog*."""
    registry = ToolRegistry()
    registry.register(build_report_tool_spec(engine, catalog))
    return registry
