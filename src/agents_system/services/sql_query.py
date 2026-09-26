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
5. Rows are bounded in BYTES by the database too (`gated_cursor`): the row
   cap alone lets one accepted query return hundreds of MB (`replace`,
   `||`; `rpad`, `string_agg` where a deployment allows them), all of it
   buffered here and handed to the model. The
   query runs as a cursor; the server measures each row and sends one
   larger than the whole budget as NULLs, and the connector fetches
   `FETCH_BATCH` rows at a time, keeping the running total itself and
   stopping at the budget. No running total lives in the database: a window
   over whole rows keeps every row in a tuplestore, which spills to
   temporary files (2 GB of disk for one call). Each FETCH runs on what is
   left of one `statement_timeout`, so the whole query stays inside it.
6. The transaction is always rolled back; nothing this path runs is ever
   committed.

The engine is a dedicated one for the tool's own role - never the
application's read-write engine or a turn-scoped session.
"""

from __future__ import annotations

import math
import re
import time
import uuid
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


_STATEMENT_TIMEOUT = text(
    "SELECT pg_catalog.set_config('statement_timeout', :statement_timeout, true)"
)
"""Sets the time left for the next FETCH. Static text, bound value (AD-2)."""

FETCH_BATCH = 10
"""Rows per FETCH. Every row the database sends fits the byte budget on its
own, so a call moves at most the budget plus one batch of rows."""

_CURSOR_NAME = re.compile(r"sql_query_rows_[0-9a-f]{32}")

_GATED_CURSOR = (
    'DECLARE "{cursor}" NO SCROLL CURSOR FOR '
    'SELECT "sql_query_row".*, "sql_query_size"."size" '
    'FROM ({query}) AS "sql_query_rows" '
    "CROSS JOIN LATERAL (SELECT pg_catalog.octet_length("
    '"sql_query_rows".*::text) AS "size") AS "sql_query_size" '
    'LEFT JOIN LATERAL (SELECT "sql_query_rows".*) AS "sql_query_row" '
    'ON "sql_query_size"."size" <= {byte_limit}'
)
"""Opens the guarded query as a cursor whose rows the database size-gates.

Each row is measured by its text form. The first lateral join expands a
row that fits the whole budget back into its own columns (names, types and
order intact); a row that does not comes back as NULLs plus its size. The
row is referenced only as `"sql_query_rows".*`, which no column of the
query can shadow (a bare `sql_query_rows` would resolve to a column of that
name first). The plan streams row by row - no window, no sort, nothing
kept - and the cursor computes only the rows the connector fetches. Static
text around the guard's validated rendering, a generated cursor name and an
integer from configuration (AD-2)."""


def new_cursor_name() -> str:
    return f"sql_query_rows_{uuid.uuid4().hex}"


def gated_cursor(sql: str, byte_limit: int, cursor: str) -> str:
    """DECLARE *cursor* over guarded *sql*, every row gated at *byte_limit*."""
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int):
        raise TypeError("byte_limit must be an int.")
    if byte_limit < 1:
        raise ValueError("byte_limit must be positive.")
    if not _CURSOR_NAME.fullmatch(cursor):
        raise ValueError("cursor must be a name from new_cursor_name().")
    return _GATED_CURSOR.format(cursor=cursor, byte_limit=byte_limit, query=sql)


def _now() -> float:
    return time.monotonic()


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
            deadline = _now() + statement_timeout_ms / 1000
            cursor = new_cursor_name()
            await conn.exec_driver_sql(gated_cursor(guarded.sql, byte_limit, cursor))
            return await _fetch_within_budget(
                conn,
                cursor,
                deadline=deadline,
                row_limit=row_limit,
                byte_limit=byte_limit,
            )
        finally:
            await conn.rollback()


async def _fetch_within_budget(
    conn: Any, cursor: str, *, deadline: float, row_limit: int, byte_limit: int
) -> QueryRows:
    """FETCH from *cursor* until the rows, the bytes or the query run out."""
    fetch = f'FETCH FORWARD {FETCH_BATCH} FROM "{cursor}"'
    columns: list[str] | None = None
    rows: list[tuple[Any, ...]] = []
    used = 0
    seen = 0
    while True:
        left_ms = max(1, int((deadline - _now()) * 1000))
        await conn.execute(_STATEMENT_TIMEOUT, {"statement_timeout": f"{left_ms}ms"})
        result = await conn.exec_driver_sql(fetch)
        if columns is None:
            # The gate appends `size` as the last column; the rest are the
            # query's own, by position (names may repeat).
            keys = result.keys()
            columns = [str(column) for column in keys][:-1]
        batch = result.fetchall()
        for *values, size in batch:
            seen += 1
            if seen > row_limit:
                return QueryRows(columns=columns, rows=rows, truncated=True)
            used += int(size)
            if used > byte_limit:
                return QueryRows(
                    columns=columns, rows=rows, truncated=True, truncated_bytes=True
                )
            rows.append(tuple(values))
        if len(batch) < FETCH_BATCH:
            return QueryRows(columns=columns, rows=rows, truncated=False)


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
