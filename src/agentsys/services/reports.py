"""Generic, client-agnostic BI report execution engine (D-023).

A `ReportSpec` is a named, statically-defined, parameterized query. The
platform NEVER lets a caller (human, LLM, or otherwise) build SQL text -
each report's SQL is fixed module-level text with SQLAlchemy named
bindparams, and every parameter is validated against its own schema before
anything touches the database (AD-7, AD-8).

AD-2 is absolute: nothing in this module ever builds a SQL string by
f-string / `.format()` / concatenation of a caller-supplied value - not even
a column or table name. A report that needs a variable dimension becomes a
separate ReportSpec, never a template hole. Client-specific catalogs (e.g.
`agentsys.connectors.badie_reports`) supply the actual `ReportSpec` values;
this module only knows how to validate parameters against a spec and run it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from sqlalchemy import TextClause
from sqlalchemy.ext.asyncio import AsyncEngine

HARD_ROW_CEILING = 500
"""Absolute ceiling on rows returned by any report, enforced in code (AD-5).

This applies regardless of what a specific report's own ParamSpec bounds
allow, so a misconfigured catalog cannot remove the safety net. A model
asking for 10,000,000 rows gets this ceiling, not an OOM.
"""


class ReportValidationError(ValueError):
    """Raised when caller-supplied report parameters fail validation.

    Always names the offending parameter and states what would have been
    valid (AD-7: fail loud and useful - never silently fall back to a
    "close enough" value).
    """


def _no_metadata(
    _validated: Mapping[str, Any], _bind_params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Default `ReportSpec.filter_metadata` - a report with no business
    filter to disclose need not override this."""
    return {}


@dataclass(frozen=True)
class ParamSpec:
    """Describes one parameter a report accepts and how to validate it.

    `name` is the caller-facing key (what a validator/LLM/tool-input
    supplies). `bind_as` is the SQL bindparam name actually used inside the
    report's static SQL - it defaults to `name` when the two are the same,
    but a report is free to expose a friendlier caller-facing name (e.g.
    `status`) than what its SQL binds (e.g. an expanding `statuses` list).
    `transform` converts the validated caller value into the bound value;
    it runs AFTER validation, never before, so validation always sees the
    real caller input.
    """

    name: str
    type: type[Any]
    default: Any = None
    required: bool = False
    minimum: int | float | None = None
    maximum: int | float | None = None
    allowed: tuple[Any, ...] | None = None
    description: str = ""
    bind_as: str | None = None
    transform: Callable[[Any], Any] | None = None

    def bind_name(self) -> str:
        return self.bind_as or self.name


@dataclass(frozen=True)
class ReportSpec:
    """A named, static, parameterized report over a catalog's own schema.

    `sql` is a pre-built `sqlalchemy.text(...)` clause (optionally with
    `.bindparams(...)` already applied for typed/expanding binds) - never a
    plain string assembled at call time. `filter_metadata` computes what to
    disclose about which business filter (e.g. order status) was actually
    applied - callers must never have to guess what a report's numbers do or
    do not include.

    It receives BOTH the validated caller values and the bind params that
    went into the SQL. The bind params are what makes a disclosure DERIVED
    rather than recomputed: a value produced by a `transform` (a time window
    from a clock read, say) exists only after `build_bind_params`, and
    recomputing it in the disclosure describes a query that was never run.
    """

    name: str
    description: str
    sql: TextClause
    params: tuple[ParamSpec, ...] = ()
    limit_param: str | None = "limit"
    filter_metadata: Callable[
        [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
    ] = _no_metadata

    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)


def clamp_row_limit(requested: int, ceiling: int = HARD_ROW_CEILING) -> int:
    """Clamp *requested* into `[1, ceiling]` (AD-5).

    This is a pure, code-level safety net independent of any report's own
    ParamSpec bounds - it runs unconditionally in `run_report`.
    """
    return max(1, min(int(requested), ceiling))


def validate_params(spec: ReportSpec, raw_params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate *raw_params* against *spec* BEFORE any execution (AD-7, AD-8).

    - Unknown parameter name -> `ReportValidationError` naming it plus every
      valid parameter name for this report.
    - Missing required parameter -> `ReportValidationError`.
    - Wrong type -> `ReportValidationError` naming the parameter and the
      expected type (bool is rejected for `int` params despite being an int
      subclass in Python).
    - Out of the declared `[minimum, maximum]` range, or not in `allowed` ->
      `ReportValidationError` stating what IS valid.

    Returns validated, defaulted values keyed by caller-facing param name -
    NOT yet clamped and NOT yet mapped onto SQL bind names. See
    `build_bind_params` for that step.
    """
    valid_names = spec.param_names()
    for key in raw_params:
        if key not in valid_names:
            raise ReportValidationError(
                f"Unknown parameter '{key}' for report '{spec.name}'. "
                f"Valid parameters: {', '.join(valid_names) or '(none)'}."
            )

    validated: dict[str, Any] = {}
    for param in spec.params:
        # An explicit `null` resolves to the default, exactly like an absent
        # key. `raw_params.get(name, default)` does NOT do this: the fallback
        # applies only when the key is MISSING, so a present key holding None
        # yields None — and None then skips every check below on its way into
        # the bound SQL. Models emit `null` constantly to mean "use the
        # default", which made that the ordinary path, not an exotic one.
        value = raw_params.get(param.name)
        if value is None:
            value = param.default

        if value is None:
            if param.required:
                raise ReportValidationError(
                    f"Missing required parameter '{param.name}' for report "
                    f"'{spec.name}'."
                )
            validated[param.name] = None
            continue

        if param.type is int and isinstance(value, bool):
            raise ReportValidationError(
                f"Parameter '{param.name}' must be of type int, got bool."
            )
        if not isinstance(value, param.type):
            raise ReportValidationError(
                f"Parameter '{param.name}' must be of type "
                f"{param.type.__name__}, got {type(value).__name__} "
                f"({value!r})."
            )
        if param.minimum is not None and value < param.minimum:
            raise ReportValidationError(
                f"Parameter '{param.name}' must be >= {param.minimum}, "
                f"got {value}."
            )
        if param.maximum is not None and value > param.maximum:
            raise ReportValidationError(
                f"Parameter '{param.name}' must be <= {param.maximum}, "
                f"got {value}."
            )
        if param.allowed is not None and value not in param.allowed:
            raise ReportValidationError(
                f"Parameter '{param.name}' must be one of "
                f"{list(param.allowed)}, got {value!r}."
            )
        validated[param.name] = value

    return validated


def build_bind_params(spec: ReportSpec, validated: Mapping[str, Any]) -> dict[str, Any]:
    """Map validated, clamped values onto their SQL bind names.

    This is where a caller-facing param (e.g. `status`) becomes what the
    static SQL actually binds (e.g. an expanding `statuses` list) via each
    ParamSpec's own declared `bind_as` + `transform` - the mapping is fixed
    on the spec, never built from caller input.
    """
    bind_params: dict[str, Any] = {}
    for param in spec.params:
        value = validated.get(param.name)
        if param.transform is not None:
            value = param.transform(value)
        bind_params[param.bind_name()] = value
    return bind_params


def json_safe(value: Any) -> Any:
    """Convert DB-native values into something `json.dumps` accepts.

    The tool result is serialized into the agent's message history with a
    plain encoder, so a `Decimal` straight out of a Postgres NUMERIC column
    raises `TypeError` and takes the whole request down with a 500.

    Money becomes `str`, deliberately, not `float`. These figures get
    reconciled against the database by a human, and `float` would introduce
    binary-representation artifacts in exactly the digits that make a
    reconciliation fail. The agent should be reporting these numbers, not
    doing arithmetic on them.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


async def fetch_rows(
    engine: AsyncEngine, spec: ReportSpec, bind_params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Execute *spec*'s static SQL with *bind_params* and return rows as dicts.

    Takes already-validated, already-clamped, already-bind-mapped
    parameters - this function performs no validation of its own. Opens and
    closes its own connection; does not commit or rollback (the SELECT-only
    statement never needs to, and the underlying role is DB-level
    read-only regardless).
    """
    async with engine.connect() as conn:
        result = await conn.execute(spec.sql, dict(bind_params))
        rows: list[dict[str, Any]] = [dict(row._mapping) for row in result]
    return [json_safe(row) for row in rows]


async def run_report(
    engine: AsyncEngine, spec: ReportSpec, raw_params: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate, clamp, execute, and attach filter metadata for *spec*.

    The single entry point connectors should call: validation (and any
    resulting `ReportValidationError`) always happens before the engine is
    ever touched.
    """
    validated = validate_params(spec, raw_params)

    if spec.limit_param is not None:
        # Unconditional, as `clamp_row_limit`'s docstring already claimed.
        # Guarding this on `is not None` meant a report whose limit ParamSpec
        # has no default bound None, and Postgres reads `LIMIT NULL` as
        # `LIMIT ALL` — the ceiling did not merely go unapplied, it vanished.
        requested = validated.get(spec.limit_param)
        validated[spec.limit_param] = clamp_row_limit(
            HARD_ROW_CEILING if requested is None else requested
        )

    bind_params = build_bind_params(spec, validated)
    rows = await fetch_rows(engine, spec, bind_params)

    return {
        "report": spec.name,
        "rows": rows,
        "row_count": len(rows),
        # `meta` rides in the same JSON payload as `rows`, so it needs the
        # same treatment: a window boundary is a datetime and a monetary
        # threshold is a Decimal, and both are natural things to disclose.
        "meta": json_safe(dict(spec.filter_metadata(validated, bind_params))),
    }
