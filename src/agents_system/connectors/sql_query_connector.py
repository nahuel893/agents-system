"""The read-only SQL tool, `sql_query` (#80, ADR-007).

The model writes the SQL. ADR-007 amends AD-2 for this tool alone and moves
the trust boundary to the database: model-authored SQL runs only inside
limits PostgreSQL itself enforces. Each layer below is meant to hold even if
every other one fails.

1. Database (the boundary). *engine* must connect as a dedicated role
   provisioned by `scripts/provision_sql_readonly.sql`: read-only by default,
   no elevated attributes, SELECT on the allowlisted views and nothing else.
   `services.sql_query.run_guarded_query` re-verifies that on every call,
   inside a `READ ONLY` transaction with a server-side, transaction-local
   `statement_timeout`, before the model's query runs, and always rolls
   back.
2. Application. `services.sql_guard` parses the text with a PostgreSQL
   parser: one plain SELECT, allowlisted relations and functions only. The
   text sent to the database is the guard's canonical rendering, wrapped with
   `LIMIT row_limit + 1`, and it goes through the driver's extended protocol
   (asyncpg prepares every statement), which refuses a second statement on
   its own. At most `row_limit + 1` rows are fetched; the extra one only
   reports truncation. Bytes are bounded too: the database blanks every row
   past `byte_limit` (a running sum of row sizes) before it is sent, and the
   JSON rows handed back are cut at the same budget (`truncated_bytes`).
   The guard itself runs in a worker thread, off the event loop.
3. Tool surface. Its own permission, `query:sql`, in its own `Query` family
   at T2, so Layer-2 revalidates every call. Error texts are fixed: none
   carries the SQL error, the driver's exception or connection details.

No predefined role equips this tool (a SemVer-relevant decision left to a
follow-up); a deployment or an importer registers it with
`build_sql_query_tool_spec`.

`session` is accepted to satisfy the connector contract and ignored: the
turn-scoped session is the application's read-write connection, the exact
thing this tool must never run on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from agents_system.harness.registry import Tier, ToolSpec
from agents_system.services.db_role import UNWRAPPED_CONNECT_ERRORS
from agents_system.services.reports import HARD_ROW_CEILING
from agents_system.services.sql_guard import (
    DEFAULT_ALLOWED_FUNCTIONS,
    QueryPolicy,
    QueryRejectedError,
    guard_query,
    parse_relation_name,
)
from agents_system.services.sql_query import (
    UnsafeQueryRoleError,
    json_cell,
    run_guarded_query,
)

_logger = structlog.get_logger(__name__)

SQL_QUERY_TOOL_NAME = "sql_query"
QUERY_SQL_PERMISSION = "query:sql"

DEFAULT_ROW_LIMIT = 100
DEFAULT_STATEMENT_TIMEOUT_MS = 5_000
MAX_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_BYTE_LIMIT = 65_536
MIN_BYTE_LIMIT = 1_024
MAX_BYTE_LIMIT = 1_048_576

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]

_NOT_CONFIGURED_MESSAGE = (
    "Ad hoc SQL is not available on this deployment - no read-only query "
    "database is bound - so no query can be run. Say so plainly: do not "
    "estimate, and do not present any number as if a query had returned."
)
_ROLE_REFUSED_MESSAGE = (
    "The query database is not configured safely for ad hoc SQL, so the "
    "query was not run. Say that the data is unavailable right now; do not "
    "estimate."
)
_DB_UNAVAILABLE_MESSAGE = (
    "The query database could not be reached, so the query did not run. Say "
    "that the data is unavailable right now; do not estimate."
)
_REFUSED_BY_DATABASE_MESSAGE = (
    "The database refused the query: this tool may only read the queryable "
    "views listed in its description."
)
_QUERY_INVALID_MESSAGE = (
    "The database could not run the query: a column, type or expression in "
    "it is not valid for the queryable views. Check the names against the "
    "view descriptions and retry with a corrected query."
)
_DATA_ERROR_MESSAGE = (
    "The database could not evaluate the query because of a data error (for "
    "example a division by zero or an invalid date or number format). Adjust "
    "the query and retry."
)
_QUERY_FAILED_MESSAGE = (
    "The database could not complete the query. Try a simpler or more "
    "selective query; do not estimate the answer."
)

_REFUSED_SQLSTATES = frozenset({"42501", "25006"})
_TIMEOUT_SQLSTATES = frozenset({"57014", "55P03"})


@dataclass(frozen=True)
class SqlQueryConfig:
    """Deployment configuration for `sql_query`.

    `views` maps each queryable `schema.view` to a short description the
    model sees (columns and grain help it write correct SQL). The same names
    must be the ONLY relations the database role can read; the tool checks
    that on every call.
    """

    views: Mapping[str, str]
    row_limit: int = DEFAULT_ROW_LIMIT
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    allowed_functions: frozenset[str] = field(default=DEFAULT_ALLOWED_FUNCTIONS)
    byte_limit: int = DEFAULT_BYTE_LIMIT

    def __post_init__(self) -> None:
        if not self.views:
            raise ValueError("SqlQueryConfig needs at least one queryable view.")
        for name in self.views:
            parse_relation_name(name)
        if not 1 <= self.row_limit <= HARD_ROW_CEILING:
            raise ValueError(
                f"row_limit must be between 1 and {HARD_ROW_CEILING} "
                "(the platform's hard row ceiling)."
            )
        if not 1 <= self.statement_timeout_ms <= MAX_STATEMENT_TIMEOUT_MS:
            raise ValueError(
                f"statement_timeout_ms must be between 1 and {MAX_STATEMENT_TIMEOUT_MS}."
            )
        if not MIN_BYTE_LIMIT <= self.byte_limit <= MAX_BYTE_LIMIT:
            raise ValueError(
                f"byte_limit must be between {MIN_BYTE_LIMIT} and {MAX_BYTE_LIMIT}."
            )

    def policy(self) -> QueryPolicy:
        return QueryPolicy(
            allowed_relations=frozenset(parse_relation_name(n) for n in self.views),
            allowed_functions=self.allowed_functions,
        )


def _describe(config: SqlQueryConfig) -> str:
    views = "\n".join(
        f"- {name}: {description}" if description else f"- {name}"
        for name, description in sorted(config.views.items())
    )
    return (
        "Run ONE read-only PostgreSQL SELECT (or WITH ... SELECT) over the "
        "queryable views below, for questions the fixed reports cannot "
        "answer. Only these views exist for this tool:\n"
        f"{views}\n"
        "Rules: a single statement; no writes, DDL, SET or transaction "
        "control; only standard aggregate, window, date, string and math "
        "functions, called by their plain name; write values as literals. "
        f"At most {config.row_limit} rows and {config.byte_limit} bytes come "
        "back (`truncated` says when more matched: aggregate or filter instead "
        "of paging; `truncated_bytes` says the byte budget cut the rows: "
        "select fewer or shorter columns, for example left(col, 200)), and "
        f"the database stops any query after {config.statement_timeout_ms / 1000:g} "
        "seconds. An empty `rows` list with `empty_result` true means the "
        "query ran and matched nothing."
    )


def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "One PostgreSQL SELECT statement over the queryable views."
                ),
            }
        },
        "required": ["sql"],
    }


def _database_error(error: SQLAlchemyError, config: SqlQueryConfig) -> ConnectorOutput:
    """Map a database failure to a fixed result; never the driver's text."""
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    sqlstate = str(sqlstate) if sqlstate else None
    unavailable = sqlstate is None or sqlstate.startswith("08")
    # Operators get the driver's detail only for connectivity failures, where
    # they need it; a query error's text quotes the query and its data, so
    # the log keeps just the SQLSTATE for those.
    _logger.warning(
        "sql_query.failed",
        sqlstate=sqlstate,
        error_class=type(error).__name__,
        exc_info=unavailable,
    )
    if sqlstate in _TIMEOUT_SQLSTATES:
        seconds = config.statement_timeout_ms / 1000
        return {
            "error": (
                f"The database stopped the query at its {seconds:g}-second "
                "limit. Narrow it (filter by date, aggregate, or read fewer "
                "rows) and retry."
            ),
            "error_kind": "query_timeout",
        }
    if sqlstate in _REFUSED_SQLSTATES:
        return {
            "error": _REFUSED_BY_DATABASE_MESSAGE,
            "error_kind": "refused_by_database",
        }
    if unavailable or sqlstate is None:
        return {"error": _DB_UNAVAILABLE_MESSAGE, "error_kind": "database_unavailable"}
    if sqlstate.startswith("42"):
        return {"error": _QUERY_INVALID_MESSAGE, "error_kind": "query_invalid"}
    if sqlstate.startswith("22"):
        return {"error": _DATA_ERROR_MESSAGE, "error_kind": "data_error"}
    return {"error": _QUERY_FAILED_MESSAGE, "error_kind": "query_failed"}


def _rows_within_budget(
    rows: list[tuple[Any, ...]], byte_limit: int
) -> tuple[list[list[Any]], bool]:
    """JSON-safe rows, cut before their JSON exceeds *byte_limit* bytes.

    The database already bounds rows by their text size; JSON escaping can
    make a row larger than that, and this is what reaches the model.
    """
    kept: list[list[Any]] = []
    used = 2  # the enclosing brackets
    for row in rows:
        cells = [json_cell(cell) for cell in row]
        used += len(json.dumps(cells)) + (2 if kept else 0)  # ", " separator
        if used > byte_limit:
            return kept, True
        kept.append(cells)
    return kept, False


def _byte_budget_note(byte_limit: int) -> str:
    return (
        f"The rows were cut at this tool's {byte_limit}-byte budget. Select "
        "fewer or shorter columns (for example left(col, 200)) or aggregate, "
        "and do not treat the rows shown as the whole result."
    )


def build_sql_query_connector(engine: Any, config: SqlQueryConfig) -> AsyncConnector:
    """Build the async `sql_query` connector over *engine*.

    *engine* must be a dedicated engine for the tool's read-only role (see the
    module docstring); None registers the tool unbound, answering with a fixed
    "not configured" result instead of failing role assembly.
    """
    policy = config.policy()
    row_limit = config.row_limit

    async def sql_query_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if engine is None:
            return {
                "error": _NOT_CONFIGURED_MESSAGE,
                "error_kind": "sql_not_configured",
            }

        try:
            # CPU-bound parsing and rendering: off the event loop, so it
            # stalls no other turn and the harness's per-call timeout keeps
            # control of this one.
            guarded = await asyncio.to_thread(
                guard_query, inputs.get("sql"), policy, row_limit=row_limit
            )
        except QueryRejectedError as rejection:
            _logger.info("sql_query.rejected", reason=rejection.code)
            return {
                "error": rejection.message,
                "error_kind": "query_rejected",
                "reason": rejection.code,
            }

        try:
            outcome = await run_guarded_query(
                engine,
                guarded,
                allowed_relations=policy.allowed_relations,
                statement_timeout_ms=config.statement_timeout_ms,
                row_limit=row_limit,
                byte_limit=config.byte_limit,
            )
        except UnsafeQueryRoleError as unsafe:
            _logger.error(
                "sql_query.role_not_read_only", problems=list(unsafe.problems)
            )
            return {"error": _ROLE_REFUSED_MESSAGE, "error_kind": "role_not_read_only"}
        except SQLAlchemyError as error:
            return _database_error(error, config)
        except UNWRAPPED_CONNECT_ERRORS as error:
            # Refused, unknown host, bad password, connect timeout: the
            # driver's own exception, never wrapped by SQLAlchemy. Operators
            # get the detail; the model gets the fixed text.
            _logger.warning(
                "sql_query.failed",
                sqlstate=None,
                error_class=type(error).__name__,
                exc_info=True,
            )
            return {
                "error": _DB_UNAVAILABLE_MESSAGE,
                "error_kind": "database_unavailable",
            }

        rows, cut = _rows_within_budget(outcome.rows, config.byte_limit)
        truncated_bytes = outcome.truncated_bytes or cut
        truncated = outcome.truncated or truncated_bytes
        output: ConnectorOutput = {
            "columns": outcome.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "truncated_bytes": truncated_bytes,
            "row_limit": row_limit,
            "byte_limit": config.byte_limit,
            "empty_result": not rows and not truncated,
            "relations": list(guarded.relations),
        }
        if truncated_bytes:
            output["note"] = _byte_budget_note(config.byte_limit)
        return output

    return sql_query_connector


def build_sql_query_tool_spec(engine: Any, config: SqlQueryConfig) -> ToolSpec:
    """Return the `sql_query` ToolSpec, unregistered.

    T2 with its own `query:sql` permission (ADR-007): revalidated on every
    call, never covered by a `read:*` grant, and - being below T3 - not
    barred from `untrusted_input` roles by R4, so granting it to one is an
    explicit deployment decision over what the allowlisted views disclose.
    """
    return ToolSpec(
        name=SQL_QUERY_TOOL_NAME,
        description=_describe(config),
        required_permissions=(QUERY_SQL_PERMISSION,),
        input_schema=_input_schema(),
        connector=build_sql_query_connector(engine, config),
        tier=Tier.T2,
    )
