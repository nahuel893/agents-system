"""Integration tests: the read-only SQL tool against a real PostgreSQL (#80).

ADR-007 puts the trust boundary for model-authored SQL in the database, so
this file is where that claim is proven rather than asserted. It needs:

- `tests/fixtures/sql_query_fixture.sql` loaded as the admin role;
- the `sql_readonly` role provisioned by `scripts/provision_sql_readonly.sql`
  with `sql_views='sql_tool_fixture.sales_v'`;
- `DATABASE_URL` (admin, used only to inspect and to flip grants) and
  `SQL_DATABASE_URL` (the `sql_readonly` role) exported.

The `bi-readonly` CI job does all three. The central test runs write and DDL
statements through the tool's own role with the application guard BYPASSED,
and asserts PostgreSQL refuses every one of them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agents_system.connectors import sql_query_connector
from agents_system.connectors.sql_query_connector import (
    SqlQueryConfig,
    build_sql_query_connector,
)
from agents_system.models.base import get_engine
from agents_system.services import sql_query
from agents_system.services.db_role import verify_query_role
from agents_system.services.sql_guard import (
    DEFAULT_ALLOWED_FUNCTIONS,
    GuardedQuery,
    guard_query,
)
from agents_system.services.sql_query import json_cell

pytestmark = pytest.mark.integration

_VIEW = "sql_tool_fixture.sales_v"
_ALLOWED = frozenset({("sql_tool_fixture", "sales_v")})
_CONFIG = SqlQueryConfig(
    views={_VIEW: "One row per sale: id, product, amount, sold_on."},
    row_limit=10,
    statement_timeout_ms=2_000,
)

#: A deployment that opts back in to the functions the default allowlist
#: leaves out because they build large values from short input: the byte
#: gate and the role's temp_file_limit must still hold for it.
_BUILDERS = DEFAULT_ALLOWED_FUNCTIONS | {"rpad", "lpad", "string_agg"}
_BUILDERS_CONFIG = SqlQueryConfig(
    views=_CONFIG.views,
    row_limit=_CONFIG.row_limit,
    statement_timeout_ms=_CONFIG.statement_timeout_ms,
    allowed_functions=_BUILDERS,
)

#: SQLSTATEs PostgreSQL uses to refuse a write: read_only_sql_transaction and
#: insufficient_privilege.
_REFUSED = {"25006", "42501"}


def _require(name: str) -> str:
    url = os.environ.get(name)
    if not url:
        pytest.skip(f"{name} not set - see this module's docstring.")
    return url


@pytest.fixture
async def sql_engine() -> AsyncIterator[AsyncEngine]:
    engine = get_engine(_require("SQL_DATABASE_URL"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    engine = get_engine(_require("DATABASE_URL"))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _admin(engine: AsyncEngine, sql: str) -> Any:
    async with engine.begin() as conn:
        result = await conn.execute(text(sql))
        return result.scalar() if result.returns_rows else None


async def _sales_row_count(admin_engine: AsyncEngine) -> int:
    return int(
        await _admin(admin_engine, "SELECT count(*) FROM sql_tool_fixture.sales")
    )


# ---------------------------------------------------------------------------
# The provisioned role and the happy path
# ---------------------------------------------------------------------------


async def test_the_provisioned_role_verifies_as_safe(sql_engine: AsyncEngine) -> None:
    assert await verify_query_role(sql_engine, _ALLOWED) is True


async def test_the_provisioned_role_caps_its_temporary_files(
    sql_engine: AsyncEngine,
) -> None:
    async with sql_engine.connect() as conn:
        limit = (await conn.exec_driver_sql("SHOW temp_file_limit")).scalar()

    assert limit == "256MB"


async def test_a_query_that_would_spill_past_the_cap_is_stopped_by_the_database(
    sql_engine: AsyncEngine,
) -> None:
    # A hash of 300 values of 1 MB spills far past work_mem; the role's
    # temp_file_limit stops it at 256 MB instead of letting it fill the disk.
    config = SqlQueryConfig(
        views=_CONFIG.views, statement_timeout_ms=20_000, allowed_functions=_BUILDERS
    )
    connector = build_sql_query_connector(sql_engine, config)

    result = await connector(
        {
            "sql": "SELECT count(*) FROM (SELECT DISTINCT rpad(g::text, 1000000, 'x') "
            "FROM generate_series(1, 300) AS g) AS d"
        }
    )

    assert result["error_kind"] == "query_failed", result


async def test_a_query_runs_under_the_read_only_role(sql_engine: AsyncEngine) -> None:
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector(
        {
            "sql": "SELECT product, sum(amount) AS total, count(*) AS n "
            "FROM sales_v GROUP BY product ORDER BY product"
        }
    )

    assert "error" not in result, result
    assert result["columns"] == ["product", "total", "n"]
    assert result["row_count"] == 5
    assert result["truncated"] is False
    assert result["rows"][0][0] == "product-0"
    assert isinstance(result["rows"][0][1], str)  # NUMERIC stays exact
    assert result["relations"] == [_VIEW]


_REPRESENTATIVE_QUERIES = (
    (
        "SELECT date_trunc('month', sold_on)::date AS month, sum(amount) AS total "
        "FROM sales_v GROUP BY 1 ORDER BY 1"
    ),
    (
        "SELECT to_char(sold_on, 'YYYY-MM') AS month, round(avg(amount), 1) "
        "FROM sales_v GROUP BY 1 ORDER BY 1"
    ),
    (
        "SELECT extract(year FROM sold_on) AS y, count(*) FILTER "
        "(WHERE amount > 10) AS big FROM sales_v GROUP BY 1"
    ),
    (
        "SELECT product, rank() OVER (ORDER BY sum(amount) DESC) AS r "
        "FROM sales_v GROUP BY product ORDER BY r, product"
    ),
    (
        "WITH top AS (SELECT product, sum(amount) AS t FROM sales_v "
        "GROUP BY product) SELECT count(*) FROM top WHERE t > 100"
    ),
    "SELECT DISTINCT ON (product) product, id FROM sales_v ORDER BY product, id DESC",
    (
        "SELECT count(*) FROM sales_v WHERE product ILIKE 'PRODUCT-1%' "
        "AND sold_on >= date '2026-01-01' + interval '10 days'"
    ),
    (
        "SELECT product, percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) "
        "FROM sales_v GROUP BY product ORDER BY product"
    ),
    (
        "SELECT d::date, coalesce(sum(s.amount), 0) FROM generate_series("
        "date '2026-01-01', date '2026-01-03', interval '1 day') AS d "
        "LEFT JOIN sales_v s ON s.sold_on = d::date GROUP BY 1 ORDER BY 1"
    ),
    (
        "SELECT product, amount FROM sales_v WHERE id IN (SELECT max(id) FROM sales_v "
        "GROUP BY product) UNION ALL SELECT 'total', sum(amount) FROM sales_v "
        "ORDER BY 1"
    ),
)


@pytest.mark.parametrize("sql", _REPRESENTATIVE_QUERIES)
async def test_representative_queries_mean_what_the_model_wrote(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine, sql: str
) -> None:
    # The guard sends its own rendering of the query, not the model's text.
    # Running the model's ORIGINAL text as the admin role must give the same
    # rows: the rendering keeps the query's meaning, not only its syntax.
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector({"sql": sql})

    async with admin_engine.connect() as conn:
        await conn.exec_driver_sql("SET search_path = sql_tool_fixture")
        raw = (await conn.exec_driver_sql(sql)).fetchall()
    expected = [[json_cell(cell) for cell in row] for row in raw]
    assert "error" not in result, result
    assert result["rows"] == expected[: _CONFIG.row_limit]


#: Queries the guard ACCEPTS although their text mentions the secret table:
#: inside a quoted identifier, a string, a nested comment, an alias and a
#: trailing line comment. Each must stay inert once rendered.
_INERT_MENTIONS = (
    (
        'SELECT 1 AS "a"" , (SELECT note FROM sql_tool_fixture.secrets) AS ""b" '
        "FROM sales_v"
    ),
    "SELECT 'x'' , (SELECT note FROM sql_tool_fixture.secrets) --' FROM sales_v",
    (
        "SELECT 1 FROM sales_v /* a /* b */ , (SELECT note FROM "
        "sql_tool_fixture.secrets) */"
    ),
    (
        'SELECT product AS "sql_tool_fixture.secrets" FROM sales_v '
        "-- , (SELECT note FROM sql_tool_fixture.secrets)"
    ),
)


def _plan_relations(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if "Relation Name" in node:
            found.add(f"{node.get('Schema')}.{node['Relation Name']}")
        for value in node.values():
            found |= _plan_relations(value)
    elif isinstance(node, list):
        for item in node:
            found |= _plan_relations(item)
    return found


@pytest.mark.parametrize("sql", _INERT_MENTIONS)
async def test_postgres_plans_accepted_text_against_the_allowlist_only(
    admin_engine: AsyncEngine, sql: str
) -> None:
    # A differential test against PostgreSQL's own parser: ask the server,
    # as the admin role that COULD read the secret table, which relations
    # the guard's rendering touches. Only the allowlisted view's base table
    # may appear - never the table the text merely mentions.
    guarded = guard_query(sql, _CONFIG.policy(), row_limit=_CONFIG.row_limit)

    async with admin_engine.connect() as conn:
        await conn.exec_driver_sql("SET search_path = ''")
        plan = (
            await conn.exec_driver_sql(f"EXPLAIN (VERBOSE, FORMAT JSON) {guarded.sql}")
        ).scalar()

    assert _plan_relations(plan) == {"sql_tool_fixture.sales"}


async def test_rows_are_capped_by_the_server_and_order_is_kept(
    sql_engine: AsyncEngine,
) -> None:
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector({"sql": "SELECT id FROM sales_v ORDER BY id DESC"})

    assert result["truncated"] is True
    assert result["row_count"] == 10
    assert [row[0] for row in result["rows"]] == list(range(40, 30, -1))


def _doubled(levels: int, base: str) -> str:
    """`s || s` through *levels* nested subqueries: no function at all."""
    query = f"(SELECT '{base}' AS s) AS l0"
    for level in range(1, levels + 1):
        query = f"(SELECT s || s AS s FROM {query}) AS l{level}"
    return f"SELECT s AS blob FROM {query}"


#: Queries whose rows fit the row cap but whose bytes do not: one row can
#: carry up to PostgreSQL's 1 GB per value, well inside the statement timeout.
#: The default allowlist leaves out the functions that do this from short
#: input, but `replace` and plain `||` still build large values.
_AMPLIFIERS = (
    ("SELECT replace('" + "x" * 1000 + "', 'x', '" + "x" * 1000 + "') AS blob", False),
    (_doubled(6, "x" * 2000), False),
    ("SELECT rpad('x', 5000000, 'x') AS c FROM generate_series(1, 20) AS g", True),
    ("SELECT lpad('', 5000000, 'x') AS blob FROM sales_v", True),
    (
        (
            "SELECT string_agg(rpad('x', 1000, 'x'), '') AS blob "
            "FROM generate_series(1, 20000) AS g"
        ),
        True,
    ),
)


@pytest.mark.parametrize("sql,opted_in", _AMPLIFIERS)
async def test_an_oversized_value_never_leaves_the_database(
    sql_engine: AsyncEngine, sql: str, opted_in: bool
) -> None:
    config = _BUILDERS_CONFIG if opted_in else _CONFIG
    connector = build_sql_query_connector(sql_engine, config)

    result = await connector({"sql": sql})

    assert "error" not in result, result
    assert result["rows"] == []
    assert result["truncated_bytes"] is True
    assert len(json.dumps(result)) < _CONFIG.byte_limit


async def test_many_small_rows_are_cut_at_the_byte_budget_in_order(
    sql_engine: AsyncEngine,
) -> None:
    config = SqlQueryConfig(views=_CONFIG.views, row_limit=100, byte_limit=16_384)
    connector = build_sql_query_connector(sql_engine, config)

    result = await connector(
        {
            "sql": f"SELECT g, '{'x' * 1000}' AS c "
            "FROM generate_series(1, 100) AS g ORDER BY g"
        }
    )

    assert result["truncated_bytes"] is True
    assert 10 <= result["row_count"] < 17
    assert [row[0] for row in result["rows"]] == list(range(1, result["row_count"] + 1))
    assert all(len(row[1]) == 1000 for row in result["rows"])
    assert len(json.dumps(result["rows"])) <= config.byte_limit


async def test_a_column_named_like_the_gate_keeps_its_own_value(
    sql_engine: AsyncEngine,
) -> None:
    # The gate once read the row through the bare name `sql_query_rows`,
    # which PostgreSQL resolves to a query column of that name first: the
    # first query failed and the second came back with the view's columns.
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    same_name = await connector({"sql": "SELECT 5 AS sql_query_rows FROM sales_v"})
    whole_row = await connector(
        {"sql": f"SELECT s AS sql_query_rows, '{'x' * 100}' AS big FROM sales_v AS s"}
    )

    assert "error" not in same_name, same_name
    assert same_name["columns"] == ["sql_query_rows"]
    assert same_name["rows"][0] == [5]
    assert "error" not in whole_row, whole_row
    assert whole_row["columns"] == ["sql_query_rows", "big"]
    assert whole_row["rows"][0][1] == "x" * 100


_WIDE_ROWS = "SELECT rpad('x', 5000000, 'x') AS c FROM generate_series(1, 20) AS g"


async def _temp_bytes(admin_engine: AsyncEngine) -> int:
    return int(
        await _admin(
            admin_engine,
            "SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()",
        )
    )


async def test_the_byte_gate_writes_no_temporary_files(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    # A running total over whole rows kept every row in a window tuplestore,
    # which spilled to disk: 100 MB here, gigabytes with larger values, on
    # the volume that also holds the data files.
    before = await _temp_bytes(admin_engine)

    result = await build_sql_query_connector(sql_engine, _BUILDERS_CONFIG)(
        {"sql": _WIDE_ROWS}
    )
    await sql_engine.dispose()  # a backend flushes its statistics as it exits

    assert result["truncated_bytes"] is True
    for _ in range(20):
        assert await _temp_bytes(admin_engine) == before
        await asyncio.sleep(0.1)


async def test_the_gated_plan_keeps_no_rows(admin_engine: AsyncEngine) -> None:
    guarded = guard_query(
        _WIDE_ROWS, _BUILDERS_CONFIG.policy(), row_limit=_CONFIG.row_limit
    )
    declare = sql_query.gated_cursor(
        guarded.sql, _CONFIG.byte_limit, sql_query.new_cursor_name()
    )

    async with admin_engine.connect() as conn:
        await conn.exec_driver_sql("SET search_path = ''")
        plan = (await conn.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {declare}")).scalar()

    assert not _plan_node_types(plan) & {"WindowAgg", "Materialize", "Sort"}


def _plan_node_types(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if "Node Type" in node:
            found.add(str(node["Node Type"]))
        for value in node.values():
            found |= _plan_node_types(value)
    elif isinstance(node, list):
        for item in node:
            found |= _plan_node_types(item)
    return found


async def test_the_database_computes_only_the_rows_the_budget_needs(
    sql_engine: AsyncEngine,
) -> None:
    # 100 rows of 20 MB each: fetched in batches, the first row already
    # spends the budget, so the server builds one batch and stops. Built in
    # full, the same query ran into the statement timeout.
    config = SqlQueryConfig(
        views=_CONFIG.views,
        row_limit=100,
        statement_timeout_ms=5_000,
        allowed_functions=_BUILDERS,
    )
    connector = build_sql_query_connector(sql_engine, config)

    started = time.monotonic()
    result = await connector(
        {
            "sql": "SELECT rpad('x', 20000000, 'x') AS c FROM generate_series(1, 100) AS g"
        }
    )

    assert "error" not in result, result
    assert result["rows"] == []
    assert result["truncated_bytes"] is True
    assert time.monotonic() - started < 5


async def test_the_statement_timeout_is_enforced_by_the_server(
    sql_engine: AsyncEngine,
) -> None:
    config = SqlQueryConfig(views=_CONFIG.views, row_limit=10, statement_timeout_ms=300)
    connector = build_sql_query_connector(sql_engine, config)

    started = time.monotonic()
    result = await connector(
        {"sql": "SELECT count(*) FROM generate_series(1, 5000000000) AS g"}
    )
    elapsed = time.monotonic() - started

    assert result["error_kind"] == "query_timeout"
    assert elapsed < 5


#: Values PostgreSQL returns but asyncpg cannot turn into Python objects: it
#: raises a plain ValueError or OverflowError, which SQLAlchemy does not wrap.
_UNDECODABLE = (
    "SELECT make_date(10000, 1, 1) AS d",
    "SELECT make_date(-5, 1, 1) AS d",
    "SELECT sold_on + 3000000 AS d FROM sales_v LIMIT 1",
    "SELECT make_timestamp(10000, 1, 1, 0, 0, 0) AS t",
    "SELECT make_interval(years => 100000000) AS i",
)


@pytest.mark.parametrize("sql", _UNDECODABLE)
async def test_a_value_the_driver_cannot_decode_is_a_result_not_an_exception(
    sql_engine: AsyncEngine, sql: str
) -> None:
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector({"sql": sql})
    # The fixed text's advice works: cast the column to text.
    as_text = await connector(
        {"sql": sql.replace("SELECT ", "SELECT (", 1).replace(" AS ", ")::text AS ", 1)}
    )
    after = await connector({"sql": "SELECT count(*) AS n FROM sales_v"})

    assert result["error_kind"] == "value_out_of_range", result
    assert "error" not in as_text, as_text
    assert after["rows"] == [[40]]


async def test_a_view_outside_the_allowlist_is_refused_before_the_database(
    sql_engine: AsyncEngine,
) -> None:
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector({"sql": "SELECT * FROM sql_tool_fixture.secrets_v"})

    assert result["error_kind"] == "query_rejected"
    assert result["reason"] == "relation_not_allowed"


async def test_an_unreachable_database_is_a_result_not_an_exception() -> None:
    # Port 1 refuses the connection: asyncpg raises ConnectionRefusedError,
    # which SQLAlchemy does not wrap on the connect path.
    engine = get_engine("postgresql+asyncpg://sql_readonly:x@127.0.0.1:1/acme")
    try:
        result = await build_sql_query_connector(engine, _CONFIG)(
            {"sql": "SELECT count(*) FROM sales_v"}
        )
        verified = await verify_query_role(engine, _ALLOWED)
    finally:
        await engine.dispose()

    assert result["error_kind"] == "database_unavailable"
    assert verified is None


async def test_a_rejected_password_is_a_result_not_an_exception() -> None:
    # The right host and role with the wrong password: asyncpg raises
    # InvalidPasswordError, which SQLAlchemy does not wrap on the connect
    # path either.
    url = make_url(_require("SQL_DATABASE_URL")).set(password="not-the-password")
    engine = get_engine(url.render_as_string(hide_password=False))
    try:
        result = await build_sql_query_connector(engine, _CONFIG)(
            {"sql": "SELECT count(*) FROM sales_v"}
        )
        verified = await verify_query_role(engine, _ALLOWED)
    finally:
        await engine.dispose()

    assert result["error_kind"] == "database_unavailable"
    assert "not-the-password" not in json.dumps(result)
    assert verified is None


# ---------------------------------------------------------------------------
# The database is the boundary: the guard bypassed, every write refused
# ---------------------------------------------------------------------------

_WRITES = (
    "INSERT INTO sql_tool_fixture.sales_v VALUES (999, 'x', 1, current_date)",
    "UPDATE sql_tool_fixture.sales_v SET amount = 0",
    "DELETE FROM sql_tool_fixture.sales_v",
    "DELETE FROM sql_tool_fixture.sales",
    "TRUNCATE sql_tool_fixture.sales",
    "DROP VIEW sql_tool_fixture.sales_v",
    "CREATE TABLE sql_tool_fixture.planted (a integer)",
    "CREATE TABLE public.planted (a integer)",
    "CREATE VIEW sql_tool_fixture.planted_v AS SELECT 1 AS a",
    "ALTER VIEW sql_tool_fixture.sales_v RENAME TO renamed_v",
    "GRANT SELECT ON sql_tool_fixture.secrets TO sql_readonly",
    "SELECT * FROM sql_tool_fixture.sales FOR UPDATE",
)
_READS_OUTSIDE_THE_ALLOWLIST = (
    "SELECT * FROM sql_tool_fixture.sales",
    "SELECT * FROM sql_tool_fixture.secrets",
    "SELECT * FROM sql_tool_fixture.secrets_v",
)


async def _sqlstate_of(engine: AsyncEngine, *statements: str) -> str | None:
    """Run *statements* in one fresh transaction as the tool's role."""
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.exec_driver_sql(statement)
            await conn.rollback()
    except DBAPIError as error:
        return str(getattr(error.orig, "sqlstate", None))
    return None


@pytest.mark.parametrize("statement", _WRITES)
async def test_the_role_refuses_writes_with_no_application_layer(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine, statement: str
) -> None:
    assert await _sqlstate_of(sql_engine, statement) in _REFUSED
    assert await _sales_row_count(admin_engine) == 40


@pytest.mark.parametrize("statement", _WRITES[:8])
async def test_privileges_still_refuse_writes_in_a_read_write_transaction(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine, statement: str
) -> None:
    # default_transaction_read_only is only a default: a session can open a
    # READ WRITE transaction. What must hold then is the missing privilege.
    sqlstate = await _sqlstate_of(sql_engine, "SET TRANSACTION READ WRITE", statement)

    assert sqlstate == "42501"
    assert await _sales_row_count(admin_engine) == 40


@pytest.mark.parametrize("statement", _READS_OUTSIDE_THE_ALLOWLIST)
async def test_the_role_cannot_read_beyond_the_allowlist(
    sql_engine: AsyncEngine, statement: str
) -> None:
    assert await _sqlstate_of(sql_engine, statement) == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM sql_tool_fixture.sales_v",
        "UPDATE sql_tool_fixture.sales SET amount = 0",
        "WITH d AS (DELETE FROM sql_tool_fixture.sales RETURNING 1) SELECT * FROM d",
        "CREATE TABLE public.planted AS SELECT 1 AS a",
    ],
)
async def test_the_connector_path_refuses_writes_with_the_guard_bypassed(
    sql_engine: AsyncEngine,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
) -> None:
    def no_guard(sql: object, policy: object, *, row_limit: int) -> GuardedQuery:
        return GuardedQuery(sql=str(sql), relations=())

    def no_gate(sql: str, byte_limit: int, cursor: str) -> str:
        return sql

    # Both application-side layers off: the guard, and the byte-gated cursor
    # (a DECLARE, which would refuse a bare write as a syntax error on its
    # own). What must refuse the write then is the database role.
    monkeypatch.setattr(sql_query_connector, "guard_query", no_guard)
    monkeypatch.setattr(sql_query, "gated_cursor", no_gate)
    connector = build_sql_query_connector(sql_engine, _CONFIG)

    result = await connector({"sql": statement})

    assert result["error_kind"] == "refused_by_database"
    assert await _sales_row_count(admin_engine) == 40
    assert (
        await _admin(admin_engine, "SELECT to_regclass('public.planted') IS NULL")
        is True
    )


# ---------------------------------------------------------------------------
# A drifted grant makes the tool refuse, instead of trusting the setup
# ---------------------------------------------------------------------------


_ON_THIS_DATABASE = (
    "DO $do$ BEGIN EXECUTE format('{statement} sql_readonly', "
    "current_database()); END $do$"
)

#: A deployment-owned function that runs as its owner and reads a table the
#: tool's role cannot. Named like an allowlisted built-in on purpose.
_CREATE_SECURITY_DEFINER = (
    "CREATE FUNCTION sql_tool_fixture.lower(x text) RETURNS SETOF text "
    "LANGUAGE sql SECURITY DEFINER "
    "AS $fn$ SELECT note FROM sql_tool_fixture.secrets $fn$"
)
_DROP_SECURITY_DEFINER = "DROP FUNCTION sql_tool_fixture.lower(text)"


@pytest.mark.parametrize(
    "grant,revoke",
    [
        (
            "GRANT INSERT ON sql_tool_fixture.sales_v TO sql_readonly",
            "REVOKE INSERT ON sql_tool_fixture.sales_v FROM sql_readonly",
        ),
        (
            "GRANT SELECT ON sql_tool_fixture.secrets_v TO sql_readonly",
            "REVOKE SELECT ON sql_tool_fixture.secrets_v FROM sql_readonly",
        ),
        (
            "ALTER ROLE sql_readonly SET default_transaction_read_only = off",
            "ALTER ROLE sql_readonly SET default_transaction_read_only = on",
        ),
        # Predefined roles grant server-side capabilities - running programs
        # and reading files on the database host - that no READ ONLY
        # transaction or relation privilege contains.
        (
            "GRANT pg_read_server_files, pg_execute_server_program TO sql_readonly",
            "REVOKE pg_read_server_files, pg_execute_server_program FROM sql_readonly",
        ),
        (
            "GRANT pg_signal_backend, pg_read_all_stats TO sql_readonly",
            "REVOKE pg_signal_backend, pg_read_all_stats FROM sql_readonly",
        ),
        (
            _ON_THIS_DATABASE.format(statement="GRANT CREATE ON DATABASE %I TO"),
            _ON_THIS_DATABASE.format(statement="REVOKE CREATE ON DATABASE %I FROM"),
        ),
        (_CREATE_SECURITY_DEFINER, _DROP_SECURITY_DEFINER),
        # PostgreSQL's default: no cap on temporary files.
        (
            "ALTER ROLE sql_readonly RESET temp_file_limit",
            "ALTER ROLE sql_readonly SET temp_file_limit = '256MB'",
        ),
        # A plain view without security_barrier.
        (
            "ALTER VIEW sql_tool_fixture.sales_v RESET (security_barrier)",
            "ALTER VIEW sql_tool_fixture.sales_v SET (security_barrier = true)",
        ),
    ],
)
async def test_an_unsafe_role_is_detected_and_the_tool_refuses(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine, grant: str, revoke: str
) -> None:
    await _admin(admin_engine, grant)
    try:
        # Role-level settings apply to new sessions only.
        await sql_engine.dispose()
        connector = build_sql_query_connector(sql_engine, _CONFIG)

        assert await verify_query_role(sql_engine, _ALLOWED) is False
        result = await connector({"sql": "SELECT count(*) FROM sales_v"})
        assert result["error_kind"] == "role_not_read_only"
    finally:
        await _admin(admin_engine, revoke)
        await sql_engine.dispose()

    assert await verify_query_role(sql_engine, _ALLOWED) is True


#: A view that hides rows by a join, a common per-audience pattern: only
#: product-1 is visible (at most 45.00); product-0 holds the hidden 50.00.
_CREATE_FILTERED_VIEW = (
    "CREATE TABLE sql_tool_fixture.visible_products (p text PRIMARY KEY)",
    "INSERT INTO sql_tool_fixture.visible_products VALUES ('product-1')",
    (
        "CREATE VIEW sql_tool_fixture.product_1_v AS SELECT s.id, s.product, s.amount "
        "FROM sql_tool_fixture.sales AS s "
        "JOIN sql_tool_fixture.visible_products AS f ON f.p = s.product"
    ),
    "GRANT SELECT ON sql_tool_fixture.product_1_v TO sql_readonly",
)
_DROP_FILTERED_VIEW = (
    "DROP VIEW IF EXISTS sql_tool_fixture.product_1_v",
    "DROP TABLE IF EXISTS sql_tool_fixture.visible_products",
)

#: An error oracle: a division by zero for any row above the threshold. The
#: visible rows stop at 45.00; only a hidden row lies above 47.
_ORACLE = (
    "SELECT count(*) AS n FROM sql_tool_fixture.product_1_v "
    "WHERE 1 / (CASE WHEN amount > 47 THEN 0 ELSE 1 END) = 1"
)


async def test_a_row_filtering_view_must_be_a_security_barrier_view(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    # Without security_barrier the planner pushes the model's predicate below
    # the view's join, onto rows the view hides: the data_error / rows
    # outcome then answers "is there a hidden row above x?", one call at a
    # time (a binary search recovered the hidden maximum in 17 calls).
    allowed = _ALLOWED | {("sql_tool_fixture", "product_1_v")}
    config = SqlQueryConfig(
        views={**_CONFIG.views, "sql_tool_fixture.product_1_v": "product-1 sales"}
    )
    connector = build_sql_query_connector(sql_engine, config)
    for statement in _CREATE_FILTERED_VIEW:
        await _admin(admin_engine, statement)
    try:
        leaky_verdict = await verify_query_role(sql_engine, allowed)
        leaky = await connector({"sql": _ORACLE})

        await _admin(
            admin_engine,
            "ALTER VIEW sql_tool_fixture.product_1_v SET (security_barrier = true)",
        )
        barrier_verdict = await verify_query_role(sql_engine, allowed)
        barrier = await connector({"sql": _ORACLE})
    finally:
        for statement in _DROP_FILTERED_VIEW:
            await _admin(admin_engine, statement)

    assert leaky_verdict is False
    assert leaky["error_kind"] == "role_not_read_only"
    assert barrier_verdict is True
    assert "error" not in barrier, barrier
    assert barrier["rows"] == [[8]]


async def test_a_qualified_security_definer_call_is_refused_at_both_layers(
    sql_engine: AsyncEngine,
    admin_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `SELECT * FROM schema.fn(...)` names a deployment-owned function, not
    # the built-in the empty search_path would resolve. The guard refuses the
    # qualified call; with the guard bypassed, the role check refuses to run
    # anything while the role can execute a SECURITY DEFINER function.
    sql = "SELECT * FROM sql_tool_fixture.lower('x')"
    await _admin(admin_engine, _CREATE_SECURITY_DEFINER)
    try:
        guarded = build_sql_query_connector(sql_engine, _CONFIG)
        refused = await guarded({"sql": sql})

        def no_guard(sql: object, policy: object, *, row_limit: int) -> GuardedQuery:
            return GuardedQuery(sql=str(sql), relations=())

        monkeypatch.setattr(sql_query_connector, "guard_query", no_guard)
        unguarded = await build_sql_query_connector(sql_engine, _CONFIG)({"sql": sql})
    finally:
        await _admin(admin_engine, _DROP_SECURITY_DEFINER)

    assert refused["error_kind"] == "query_rejected"
    assert refused["reason"] == "function_not_allowed"
    assert unguarded["error_kind"] == "role_not_read_only"
    assert "never readable" not in json.dumps(unguarded)


# ---------------------------------------------------------------------------
# Allowlisted base tables (opt-in): a table hides no rows, so the role's
# privileges are the whole boundary
# ---------------------------------------------------------------------------

_TABLE = "sql_tool_fixture.sales"
_TABLE_KEY = ("sql_tool_fixture", "sales")
_WITH_TABLE = _ALLOWED | {_TABLE_KEY}
_TABLE_CONFIG = SqlQueryConfig(
    views={**_CONFIG.views, _TABLE: "One row per sale (the base table)."},
    row_limit=10,
    statement_timeout_ms=2_000,
    allow_base_tables=True,
)
_TABLE_DEFAULT_CONFIG = SqlQueryConfig(
    views=_TABLE_CONFIG.views, row_limit=10, statement_timeout_ms=2_000
)


async def test_an_allowlisted_base_table_needs_the_opt_in(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    await _admin(admin_engine, f"GRANT SELECT ON {_TABLE} TO sql_readonly")
    try:
        default_verdict = await verify_query_role(sql_engine, _WITH_TABLE)
        opted_in_verdict = await verify_query_role(
            sql_engine, _WITH_TABLE, allow_base_tables=True
        )
        refused = await build_sql_query_connector(sql_engine, _TABLE_DEFAULT_CONFIG)(
            {"sql": f"SELECT count(*) AS n FROM {_TABLE}"}
        )
        answered = await build_sql_query_connector(sql_engine, _TABLE_CONFIG)(
            {"sql": f"SELECT count(*) AS n FROM {_TABLE}"}
        )
    finally:
        await _admin(admin_engine, f"REVOKE SELECT ON {_TABLE} FROM sql_readonly")

    assert default_verdict is False
    assert opted_in_verdict is True
    assert refused["error_kind"] == "role_not_read_only"
    assert "error" not in answered, answered
    assert answered["rows"] == [[40]]
    assert await verify_query_role(sql_engine, _ALLOWED) is True


async def test_the_opt_in_still_refuses_a_table_outside_the_allowlist(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    await _admin(
        admin_engine, "GRANT SELECT ON sql_tool_fixture.secrets TO sql_readonly"
    )
    try:
        verdict = await verify_query_role(sql_engine, _ALLOWED, allow_base_tables=True)
        result = await build_sql_query_connector(sql_engine, _TABLE_CONFIG)(
            {"sql": "SELECT count(*) FROM sql_tool_fixture.sales_v"}
        )
    finally:
        await _admin(
            admin_engine, "REVOKE SELECT ON sql_tool_fixture.secrets FROM sql_readonly"
        )

    assert verdict is False
    assert result["error_kind"] == "role_not_read_only"


async def test_the_opt_in_still_refuses_a_plain_view_without_the_barrier(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    await _admin(
        admin_engine,
        "ALTER VIEW sql_tool_fixture.sales_v RESET (security_barrier)",
    )
    try:
        verdict = await verify_query_role(sql_engine, _ALLOWED, allow_base_tables=True)
    finally:
        await _admin(
            admin_engine,
            "ALTER VIEW sql_tool_fixture.sales_v SET (security_barrier = true)",
        )

    assert verdict is False


async def test_a_write_grant_on_an_allowlisted_table_is_refused_with_the_opt_in(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    await _admin(admin_engine, f"GRANT SELECT, UPDATE ON {_TABLE} TO sql_readonly")
    try:
        verdict = await verify_query_role(
            sql_engine, _WITH_TABLE, allow_base_tables=True
        )
    finally:
        await _admin(admin_engine, f"REVOKE ALL ON {_TABLE} FROM sql_readonly")

    assert verdict is False


_CREATE_PARTITIONED = (
    (
        "CREATE TABLE sql_tool_fixture.events (id integer, day date) "
        "PARTITION BY RANGE (day)"
    ),
    (
        "CREATE TABLE sql_tool_fixture.events_2026 "
        "PARTITION OF sql_tool_fixture.events "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    ),
    (
        "INSERT INTO sql_tool_fixture.events "
        "SELECT g, date '2026-01-01' + g FROM generate_series(1, 12) AS g"
    ),
    "GRANT SELECT ON sql_tool_fixture.events TO sql_readonly",
)
_DROP_PARTITIONED = "DROP TABLE IF EXISTS sql_tool_fixture.events"


async def test_a_partitioned_table_is_read_through_its_parent_only(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    # GRANT does not recurse into partitions, and reading through the parent
    # checks the parent only: the parent is the allowlisted relation, and a
    # partition the role could read directly would be a finding of its own.
    allowed = _ALLOWED | {("sql_tool_fixture", "events")}
    config = SqlQueryConfig(
        views={**_CONFIG.views, "sql_tool_fixture.events": "Events by day."},
        row_limit=10,
        statement_timeout_ms=2_000,
        allow_base_tables=True,
    )
    for statement in _CREATE_PARTITIONED:
        await _admin(admin_engine, statement)
    try:
        default_verdict = await verify_query_role(sql_engine, allowed)
        verdict = await verify_query_role(sql_engine, allowed, allow_base_tables=True)
        answered = await build_sql_query_connector(sql_engine, config)(
            {"sql": "SELECT count(*) AS n FROM sql_tool_fixture.events"}
        )
        direct = await _sqlstate_of(
            sql_engine, "SELECT count(*) FROM sql_tool_fixture.events_2026"
        )
        await _admin(
            admin_engine,
            "GRANT SELECT ON sql_tool_fixture.events_2026 TO sql_readonly",
        )
        leaky_verdict = await verify_query_role(
            sql_engine, allowed, allow_base_tables=True
        )
    finally:
        await _admin(admin_engine, _DROP_PARTITIONED)

    assert default_verdict is False
    assert verdict is True
    assert answered["rows"] == [[12]]
    assert direct == "42501"
    assert leaky_verdict is False


_CREATE_RLS_TABLE = (
    (
        "CREATE TABLE sql_tool_fixture.rls_sales AS "
        "SELECT id, product, amount FROM sql_tool_fixture.sales"
    ),
    "ALTER TABLE sql_tool_fixture.rls_sales ENABLE ROW LEVEL SECURITY",
    (
        "CREATE POLICY only_product_1 ON sql_tool_fixture.rls_sales "
        "FOR SELECT TO sql_readonly USING (product = 'product-1')"
    ),
    "GRANT SELECT ON sql_tool_fixture.rls_sales TO sql_readonly",
)
_DROP_RLS_TABLE = "DROP TABLE IF EXISTS sql_tool_fixture.rls_sales"


async def test_row_level_security_still_hides_rows_and_is_no_error_oracle(
    sql_engine: AsyncEngine, admin_engine: AsyncEngine
) -> None:
    # The role has no BYPASSRLS and owns nothing, so the policy applies to
    # it, and PostgreSQL evaluates a policy's expression as a security
    # barrier: the model's condition never runs on a row the policy hides.
    # product-1 rows stop at 45.00; the hidden maximum is 50.00.
    key = ("sql_tool_fixture", "rls_sales")
    config = SqlQueryConfig(
        views={**_CONFIG.views, "sql_tool_fixture.rls_sales": "product-1 sales."},
        row_limit=10,
        statement_timeout_ms=2_000,
        allow_base_tables=True,
    )
    connector = build_sql_query_connector(sql_engine, config)
    for statement in _CREATE_RLS_TABLE:
        await _admin(admin_engine, statement)
    try:
        verdict = await verify_query_role(
            sql_engine, _ALLOWED | {key}, allow_base_tables=True
        )
        visible = await connector(
            {"sql": "SELECT count(*) AS n FROM sql_tool_fixture.rls_sales"}
        )
        hidden_probe = await connector(
            {
                "sql": "SELECT count(*) AS n FROM sql_tool_fixture.rls_sales "
                "WHERE 1 / (CASE WHEN amount > 47 THEN 0 ELSE 1 END) = 1"
            }
        )
        visible_probe = await connector(
            {
                "sql": "SELECT count(*) AS n FROM sql_tool_fixture.rls_sales "
                "WHERE 1 / (CASE WHEN amount > 44 THEN 0 ELSE 1 END) = 1"
            }
        )
    finally:
        await _admin(admin_engine, _DROP_RLS_TABLE)

    assert verdict is True
    assert visible["rows"] == [[8]]
    # No division by zero: the hidden 50.00 was never evaluated.
    assert "error" not in hidden_probe, hidden_probe
    assert hidden_probe["rows"] == [[8]]
    # The same probe over a value the policy shows does raise: the oracle
    # is only closed for hidden rows, which is the property that matters.
    assert visible_probe["error_kind"] == "data_error"


# ---------------------------------------------------------------------------
# The provisioning script: a listed table needs `-v sql_allow_tables=on`
# ---------------------------------------------------------------------------

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision_sql_readonly.sql"


def _psql_url(name: str) -> str:
    url = make_url(_require(name)).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _provision(*variables: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("psql") is None:
        pytest.skip("psql is not installed.")
    password = make_url(_require("SQL_DATABASE_URL")).password or ""
    args = ["-v", f"sql_password={password}"]
    for variable in variables:
        args += ["-v", variable]
    return subprocess.run(
        [
            "psql",
            _psql_url("DATABASE_URL"),
            "-v",
            "ON_ERROR_STOP=1",
            *args,
            "-f",
            str(_SCRIPT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def restore_provisioning() -> Iterator[None]:
    """Whatever a test provisioned, end on the suite's standard role."""
    yield
    restored = _provision("sql_views=sql_tool_fixture.sales_v")
    assert restored.returncode == 0, restored.stderr


_TABLE_VIEWS = "sql_views=sql_tool_fixture.sales_v,sql_tool_fixture.sales"


@pytest.mark.usefixtures("restore_provisioning")
async def test_the_script_refuses_a_listed_table_by_default(
    sql_engine: AsyncEngine,
) -> None:
    refused = _provision(_TABLE_VIEWS)

    assert refused.returncode != 0
    assert "sql_tool_fixture.sales" in refused.stderr
    assert "sql_allow_tables" in refused.stderr
    # Atomic: the role is exactly what it was, and no table was granted.
    assert await verify_query_role(sql_engine, _ALLOWED) is True


@pytest.mark.usefixtures("restore_provisioning")
async def test_the_script_grants_a_listed_table_with_the_opt_in(
    sql_engine: AsyncEngine,
) -> None:
    granted = _provision(_TABLE_VIEWS, "sql_allow_tables=on")
    await sql_engine.dispose()

    assert granted.returncode == 0, granted.stderr
    assert await verify_query_role(sql_engine, _WITH_TABLE) is False
    assert (
        await verify_query_role(sql_engine, _WITH_TABLE, allow_base_tables=True) is True
    )
    answered = await build_sql_query_connector(sql_engine, _TABLE_CONFIG)(
        {"sql": f"SELECT count(*) AS n FROM {_TABLE}"}
    )
    assert answered["rows"] == [[40]]


@pytest.mark.usefixtures("restore_provisioning")
async def test_the_script_still_refuses_a_plain_view_with_the_opt_in() -> None:
    # secrets_v is a plain view without security_barrier.
    refused = _provision("sql_views=sql_tool_fixture.secrets_v", "sql_allow_tables=on")

    assert refused.returncode != 0
    assert "security_barrier" in refused.stderr


@pytest.mark.usefixtures("restore_provisioning")
async def test_the_script_rejects_a_malformed_opt_in() -> None:
    refused = _provision(_TABLE_VIEWS, "sql_allow_tables=maybe")

    assert refused.returncode != 0
    assert "sql_allow_tables must be on or off" in refused.stderr
