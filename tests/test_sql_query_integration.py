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
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
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
from agents_system.services.sql_guard import GuardedQuery, guard_query
from agents_system.services.sql_query import json_cell

pytestmark = pytest.mark.integration

_VIEW = "sql_tool_fixture.sales_v"
_ALLOWED = frozenset({("sql_tool_fixture", "sales_v")})
_CONFIG = SqlQueryConfig(
    views={_VIEW: "One row per sale: id, product, amount, sold_on."},
    row_limit=10,
    statement_timeout_ms=2_000,
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
    config = SqlQueryConfig(views=_CONFIG.views, statement_timeout_ms=20_000)
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
    "SELECT string_agg(DISTINCT product, ',' ORDER BY product) FROM sales_v",
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


#: Queries whose rows fit the row cap but whose bytes do not: one row can
#: carry up to PostgreSQL's 1 GB per value, well inside the statement timeout.
_AMPLIFIERS = (
    "SELECT rpad('x', 5000000, 'x') AS c FROM generate_series(1, 20) AS g",
    "SELECT lpad('', 5000000, 'x') AS blob FROM sales_v",
    (
        "SELECT string_agg(rpad('x', 1000, 'x'), '') AS blob "
        "FROM generate_series(1, 20000) AS g"
    ),
)


@pytest.mark.parametrize("sql", _AMPLIFIERS)
async def test_an_oversized_value_never_leaves_the_database(
    sql_engine: AsyncEngine, sql: str
) -> None:
    connector = build_sql_query_connector(sql_engine, _CONFIG)

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
            "sql": "SELECT g, rpad('x', 1000, 'x') AS c "
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
        {
            "sql": "SELECT s AS sql_query_rows, rpad('x', 100, 'x') AS big "
            "FROM sales_v AS s"
        }
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

    result = await build_sql_query_connector(sql_engine, _CONFIG)({"sql": _WIDE_ROWS})
    await sql_engine.dispose()  # a backend flushes its statistics as it exits

    assert result["truncated_bytes"] is True
    for _ in range(20):
        assert await _temp_bytes(admin_engine) == before
        await asyncio.sleep(0.1)


async def test_the_gated_plan_keeps_no_rows(admin_engine: AsyncEngine) -> None:
    guarded = guard_query(_WIDE_ROWS, _CONFIG.policy(), row_limit=_CONFIG.row_limit)
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
        views=_CONFIG.views, row_limit=100, statement_timeout_ms=5_000
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
