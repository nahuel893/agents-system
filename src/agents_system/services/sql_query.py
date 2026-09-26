"""Execution of a guarded, model-authored query (#80, ADR-007).

`services.sql_guard` decides WHAT may run; this module decides HOW it runs,
and every step here is a database-side limit rather than an application
check:

1. `SET TRANSACTION READ ONLY` before anything else in the transaction.
2. Transaction-local `statement_timeout`, `lock_timeout`,
   `idle_in_transaction_session_timeout` and an empty `search_path`
   (`set_config(..., is_local => true)`): the server stops a slow query, and
   none of it survives into the next pooled use of the connection.
3. `check_query_role` inside the same transaction, before the query: the
   role must still be read-only by default, hold no elevated attribute, and
   be able to read nothing but the allowlisted views. Grants drift; this is
   checked on every call, not once at boot.
4. The guard's canonical rendering is sent through the driver's extended
   protocol (asyncpg prepares every statement), which refuses a second
   statement independently of the guard. At most `row_limit + 1` rows are
   fetched, and the wrapped `LIMIT` already stops the server there.
5. Rows are bounded in BYTES by the database too (`byte_gated`): the row cap
   alone lets one accepted query return hundreds of MB (`rpad`,
   `string_agg`), all of it buffered here and handed to the model. The
   server measures each row and sends every row past the running budget as
   NULLs, so an oversized value never leaves PostgreSQL.
6. The transaction is always rolled back; nothing this path runs is ever
   committed.

The engine is a dedicated one for the tool's own role - never the
application's read-write engine or a turn-scoped session.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents_system.services.db_role import check_query_role
from agents_system.services.sql_guard import GuardedQuery

LOCK_TIMEOUT_MS = 1_000

_SESSION_LIMITS = text(
    "SELECT pg_catalog.set_config('statement_timeout', :statement_timeout, true),"
    " pg_catalog.set_config('lock_timeout', :lock_timeout, true),"
    " pg_catalog.set_config('idle_in_transaction_session_timeout',"
    " :idle_timeout, true),"
    " pg_catalog.set_config('search_path', '', true),"
    " pg_catalog.set_config('standard_conforming_strings', 'on', true)"
)
"""Transaction-local limits, set before any model-authored SQL.

An empty `search_path` means an unqualified name can only resolve to a
built-in: the guard already schema-qualifies every relation, and a
deployment-owned function or operator can never shadow a built-in.
`standard_conforming_strings` is pinned because the guard's rendering relies
on one string-escaping rule (a doubled quote, backslashes literal); a
session with it turned off would read the same text differently. Static
text with bound values: AD-2 still holds for everything this module writes."""


_BYTE_GATE = (
    'SELECT "sql_query_row".*, "sql_query_gate"."fits" FROM ('
    'SELECT "sql_query_rows" AS "rec", pg_catalog.sum('
    'pg_catalog.octet_length("sql_query_rows"::text)) OVER ('
    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) <= {byte_limit} "
    'AS "fits" FROM ({query}) AS "sql_query_rows") AS "sql_query_gate" '
    'LEFT JOIN LATERAL (SELECT ("sql_query_gate"."rec").*) AS "sql_query_row" '
    'ON "sql_query_gate"."fits"'
)
"""Wraps the guarded query so the database enforces a byte budget.

A running sum of each row's size (its text form) marks whether the row
still fits; the lateral join expands a fitting row back into its own
columns, names and types intact, and turns every other row into NULLs plus
`fits = false`. Row order is the guarded query's: the window has no ORDER
BY and the join keeps its outer order. Static text around the guard's
validated rendering and an integer from configuration (AD-2)."""


def byte_gated(sql: str, byte_limit: int) -> str:
    """Wrap guarded *sql* so no row past *byte_limit* bytes leaves the server."""
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int):
        raise TypeError("byte_limit must be an int.")
    if byte_limit < 1:
        raise ValueError("byte_limit must be positive.")
    return _BYTE_GATE.format(byte_limit=byte_limit, query=sql)


class UnsafeQueryRoleError(RuntimeError):
    """The tool's database role is not safe for model-authored SQL.

    `problems` is operator detail (which relation is writable, which view is
    readable without being allowlisted); it belongs in logs, not in anything
    the model sees.
    """

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class QueryRows:
    """Raw query output: column names, at most `row_limit` rows, truncation.

    `truncated` means more rows matched than `row_limit`; `truncated_bytes`
    means the rows were cut at the byte budget.
    """

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    truncated_bytes: bool = False


def session_limits(statement_timeout_ms: int) -> dict[str, str]:
    """Bound values for `_SESSION_LIMITS`, derived from one timeout."""
    return {
        "statement_timeout": f"{statement_timeout_ms}ms",
        "lock_timeout": f"{min(LOCK_TIMEOUT_MS, statement_timeout_ms)}ms",
        "idle_timeout": f"{statement_timeout_ms * 2}ms",
    }


async def run_guarded_query(
    engine: Any,
    guarded: GuardedQuery,
    *,
    allowed_relations: frozenset[tuple[str, str]],
    statement_timeout_ms: int,
    row_limit: int,
    byte_limit: int,
) -> QueryRows:
    """Run *guarded* under the database-side limits described above.

    Raises `UnsafeQueryRoleError` when the role check fails (the query is not
    run), and lets `SQLAlchemyError` propagate for the caller to map to a
    fixed, model-safe result.
    """
    async with engine.connect() as conn:
        try:
            await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            await conn.execute(_SESSION_LIMITS, session_limits(statement_timeout_ms))
            role = await check_query_role(conn, allowed_relations)
            if not role.safe:
                raise UnsafeQueryRoleError(role.problems)
            result = await conn.exec_driver_sql(byte_gated(guarded.sql, byte_limit))
            # The gate appends `fits` as the last column; the rest are the
            # query's own, by position (names may repeat).
            keys = result.keys()
            columns = [str(column) for column in keys][:-1]
            fetched = list(result.fetchmany(row_limit + 1))
        finally:
            await conn.rollback()
    rows: list[tuple[Any, ...]] = []
    truncated_bytes = False
    for *values, fits in fetched[:row_limit]:
        if not fits:
            truncated_bytes = True
            break
        rows.append(tuple(values))
    return QueryRows(
        columns=columns,
        rows=rows,
        truncated=len(fetched) > row_limit or truncated_bytes,
        truncated_bytes=truncated_bytes,
    )


def json_cell(value: Any) -> Any:
    """Make one result cell safe for a plain JSON encoder.

    Money stays a string, for the reason `reports.json_safe` gives: no
    binary-float artifacts in figures a human reconciles. Non-finite floats
    become strings because strict JSON has no NaN or Infinity; bytes use
    PostgreSQL's own hex form; anything else unknown (UUID, time, interval,
    network types) becomes its string form rather than a serialization
    failure that would take the whole turn down.
    """
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return "\\x" + bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(key): json_cell(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_cell(item) for item in value]
    return str(value)
