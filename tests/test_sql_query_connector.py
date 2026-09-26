"""Offline tests for the read-only SQL tool connector (#80, ADR-007).

No database: a fake engine records every statement the connector sends, so
these tests pin the transaction discipline (READ ONLY first, server-side
timeouts, role verification before the model's query, always rolled back),
the row cap, JSON safety, the fixed error texts, and the tool's permission
and tier. `tests/test_sql_query_integration.py` proves the same behavior
against a real PostgreSQL and a real read-only role.
"""

from __future__ import annotations

import json
import pathlib
import socket
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Self

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from agents_system.connectors import sql_query_connector
from agents_system.connectors.sql_query_connector import (
    QUERY_SQL_PERMISSION,
    SQL_QUERY_TOOL_NAME,
    SqlQueryConfig,
    build_sql_query_connector,
    build_sql_query_tool_spec,
)
from agents_system.harness.interceptor import _is_sensitive
from agents_system.harness.loader import RootConfig, resolve
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.permissions import (
    PermissionFloorViolationError,
    PermissionTierMismatchError,
    UntrustedInputGrantError,
    builtins,
)
from agents_system.permissions.permission_registry import permission_registry
from agents_system.services import sql_query
from agents_system.services.db_role import QueryRoleCheck

_CONFIG = SqlQueryConfig(
    views={
        "reporting.sales_v": "One row per invoice line: sold_at, product, amount.",
        "reporting.clients_v": "One row per client: id, name.",
    },
    row_limit=3,
    statement_timeout_ms=1500,
)

_DRIVER_SECRET = "password=hunter2 host=db.internal relation secret_table"


class _Result:
    def __init__(self, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        self._columns = list(columns)
        self._rows = [tuple(row) for row in rows]

    def keys(self) -> list[str]:
        return list(self._columns)

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._rows[:size]


class _Connection:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._engine.closed = True

    async def exec_driver_sql(self, sql: str) -> _Result:
        self._engine.statements.append(sql)
        if sql == "SET TRANSACTION READ ONLY":
            return _Result([], [])
        if self._engine.query_error is not None:
            raise self._engine.query_error
        if "octet_length" not in sql:
            return _Result(self._engine.columns, self._engine.rows)
        # The byte-budget gate: the database appends whether each row still
        # fits the budget, and sends a row that does not as all NULLs.
        fits = self._engine.fits or [True] * len(self._engine.rows)
        return _Result(
            [*self._engine.columns, "fits"],
            [
                (*(row if fit else [None] * len(row)), fit)
                for row, fit in zip(self._engine.rows, fits, strict=True)
            ],
        )

    async def execute(
        self, clause: Any, params: Mapping[str, Any] | None = None
    ) -> Any:
        self._engine.statements.append(str(clause))
        self._engine.params.append(dict(params or {}))
        return _Result([], [])

    async def rollback(self) -> None:
        self._engine.rolled_back = True


class _Engine:
    def __init__(
        self,
        *,
        columns: Sequence[str] = ("product", "amount"),
        rows: Sequence[Sequence[Any]] = (),
        query_error: Exception | None = None,
        connect_error: Exception | None = None,
        fits: Sequence[bool] | None = None,
    ) -> None:
        self.columns = columns
        self.rows = rows
        self.fits = fits
        self.query_error = query_error
        self.connect_error = connect_error
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.rolled_back = False
        self.closed = False

    def connect(self) -> _Connection:
        if self.connect_error is not None:
            raise self.connect_error
        return _Connection(self)


@pytest.fixture(autouse=True)
def safe_role(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Default: the role check passes. Tests that need it to fail override."""
    calls: list[Any] = []

    async def fake_check(conn: Any, allowed: Any) -> QueryRoleCheck:
        calls.append(allowed)
        return QueryRoleCheck(problems=(), warnings=())

    monkeypatch.setattr(sql_query, "check_query_role", fake_check)
    return calls


def _db_error(sqlstate: str | None) -> DBAPIError:
    orig = Exception(_DRIVER_SECRET)
    orig.sqlstate = sqlstate  # type: ignore[attr-defined]
    return DBAPIError(f"SELECT 1 -- {_DRIVER_SECRET}", {}, orig)


async def _run(engine: _Engine, sql: object) -> dict[str, Any]:
    connector = build_sql_query_connector(engine, _CONFIG)
    return await connector({"sql": sql}, session=object())


# ---------------------------------------------------------------------------
# Transaction discipline
# ---------------------------------------------------------------------------


async def test_the_transaction_is_read_only_before_anything_else_runs() -> None:
    engine = _Engine(rows=[("a", 1)])
    await _run(engine, "SELECT product, amount FROM sales_v")

    assert engine.statements[0] == "SET TRANSACTION READ ONLY"


async def test_server_side_limits_are_set_per_statement_and_transaction_local() -> None:
    engine = _Engine(rows=[("a", 1)])
    await _run(engine, "SELECT product, amount FROM sales_v")

    limits_sql = engine.statements[1]
    assert "set_config('statement_timeout'" in limits_sql
    assert "set_config('search_path'" in limits_sql
    # The guard's rendering assumes one string-escaping rule; a session that
    # turned this off would read backslashes in '...' as escapes.
    assert "set_config('standard_conforming_strings', 'on', true)" in limits_sql
    assert "true" in limits_sql  # is_local: the settings die with the txn
    assert engine.params[0]["statement_timeout"] == "1500ms"


async def test_the_role_is_verified_before_the_model_query_runs(
    safe_role: list[Any],
) -> None:
    engine = _Engine(rows=[("a", 1)])
    await _run(engine, "SELECT product, amount FROM sales_v")

    assert safe_role == [
        frozenset({("reporting", "sales_v"), ("reporting", "clients_v")})
    ]


async def test_the_model_query_runs_as_the_guarded_rendering_and_is_rolled_back() -> (
    None
):
    engine = _Engine(rows=[("a", 1)])
    await _run(engine, "SELECT product, amount FROM sales_v -- note")

    executed = engine.statements[-1]
    assert '"reporting"."sales_v"' in executed
    assert "note" not in executed
    assert "LIMIT 4" in executed
    assert engine.rolled_back


async def test_the_guard_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard is CPU-bound. On the event loop it would stall every other
    # turn, and the harness's per-call asyncio.timeout could not interrupt it.
    real_guard = sql_query_connector.guard_query
    threads: list[int] = []

    def recording_guard(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(sql_query_connector, "guard_query", recording_guard)

    result = await _run(_Engine(rows=[("a", 1)]), "SELECT product FROM sales_v")

    assert "error" not in result
    assert threads
    assert threads[0] != threading.get_ident()


async def test_the_session_argument_is_never_used() -> None:
    class _Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"session.{name} touched")

    engine = _Engine(rows=[("a", 1)])
    connector = build_sql_query_connector(engine, _CONFIG)
    result = await connector(
        {"sql": "SELECT product FROM sales_v"}, session=_Exploding()
    )

    assert "error" not in result


# ---------------------------------------------------------------------------
# Refusals before the database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM reporting.sales_v",
        "SELECT 1 FROM sales_v; DROP TABLE x",
        "SELECT * FROM secret",
        "SELECT pg_sleep(30)",
        None,
    ],
)
async def test_a_rejected_query_never_opens_a_connection(sql: object) -> None:
    engine = _Engine(connect_error=AssertionError("must not connect"))

    result = await _run(engine, sql)

    assert result["error_kind"] == "query_rejected"
    assert result["reason"]
    assert engine.statements == []


async def test_an_unsafe_role_refuses_every_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unsafe(conn: Any, allowed: Any) -> QueryRoleCheck:
        return QueryRoleCheck(problems=("can write reporting.sales_v",), warnings=())

    monkeypatch.setattr(sql_query, "check_query_role", unsafe)
    engine = _Engine(rows=[("a", 1)])

    result = await _run(engine, "SELECT product FROM sales_v")

    assert result["error_kind"] == "role_not_read_only"
    assert "reporting.sales_v" not in result["error"]  # operator detail stays in logs
    # The model's query never ran: only the transaction setup did.
    assert not any('"reporting"."sales_v"' in s for s in engine.statements)
    assert engine.rolled_back


async def test_no_engine_means_not_configured() -> None:
    connector = build_sql_query_connector(None, _CONFIG)
    result = await connector({"sql": "SELECT product FROM sales_v"})
    assert result["error_kind"] == "sql_not_configured"


# ---------------------------------------------------------------------------
# Row cap and result shape
# ---------------------------------------------------------------------------


async def test_rows_are_capped_and_truncation_is_reported() -> None:
    engine = _Engine(rows=[("a", 1), ("b", 2), ("c", 3), ("d", 4)])

    result = await _run(engine, "SELECT product, amount FROM sales_v")

    assert result["row_count"] == 3
    assert result["rows"] == [["a", 1], ["b", 2], ["c", 3]]
    assert result["truncated"] is True
    assert result["row_limit"] == 3
    assert result["columns"] == ["product", "amount"]


async def test_exactly_the_cap_is_not_truncation() -> None:
    engine = _Engine(rows=[("a", 1), ("b", 2), ("c", 3)])
    result = await _run(engine, "SELECT product, amount FROM sales_v")
    assert result["truncated"] is False


async def test_the_database_gates_rows_by_a_byte_budget() -> None:
    # The row cap bounds rows, not bytes: rpad/string_agg can put hundreds of
    # MB in one cell. The executed statement measures each row's size in the
    # database and blanks every row past the running budget there, so an
    # oversized value never crosses the wire.
    engine = _Engine(rows=[("a", 1)])

    await _run(engine, "SELECT product, amount FROM sales_v")

    executed = engine.statements[-1]
    assert "pg_catalog.octet_length(" in executed
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in executed
    assert f"<= {_CONFIG.byte_limit}" in executed
    assert "LEFT JOIN LATERAL" in executed


@pytest.mark.parametrize("byte_limit", [0, -1, True, 1.5, "65536"])
def test_the_byte_gate_takes_only_a_positive_integer(byte_limit: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        sql_query.byte_gated("SELECT 1", byte_limit)


async def test_rows_past_the_byte_budget_are_cut_and_reported() -> None:
    engine = _Engine(rows=[("a", 1), ("b", 2), ("c", 3)], fits=[True, False, False])

    result = await _run(engine, "SELECT product, amount FROM sales_v")

    assert result["rows"] == [["a", 1]]
    assert result["columns"] == ["product", "amount"]
    assert result["row_count"] == 1
    assert result["truncated"] is True
    assert result["truncated_bytes"] is True
    assert result["byte_limit"] == _CONFIG.byte_limit
    assert str(_CONFIG.byte_limit) in result["note"]
    assert result["empty_result"] is False


async def test_a_first_row_over_the_budget_is_not_an_empty_result() -> None:
    engine = _Engine(rows=[("a", 1)], fits=[False])

    result = await _run(engine, "SELECT product, amount FROM sales_v")

    assert result["rows"] == []
    assert result["truncated_bytes"] is True
    assert result["empty_result"] is False


async def test_a_result_within_the_budget_is_not_cut() -> None:
    result = await _run(
        _Engine(rows=[("a", 1), ("b", 2)]), "SELECT product, amount FROM sales_v"
    )

    assert result["truncated_bytes"] is False
    assert "note" not in result


async def test_the_json_rows_never_exceed_the_byte_budget() -> None:
    # The database measures a row by its text form; JSON escaping can make
    # it larger. The connector enforces the budget on the JSON it returns.
    config = SqlQueryConfig(views=_CONFIG.views, row_limit=10, byte_limit=1_024)
    engine = _Engine(columns=("c",), rows=[("\x01" * 100,)] * 5)
    connector = build_sql_query_connector(engine, config)

    result = await connector({"sql": "SELECT product FROM sales_v"})

    assert 0 < result["row_count"] < 5
    assert len(json.dumps(result["rows"])) <= config.byte_limit
    assert result["truncated_bytes"] is True


async def test_zero_rows_is_flagged_as_an_empty_result() -> None:
    result = await _run(_Engine(rows=[]), "SELECT product, amount FROM sales_v")

    assert result["empty_result"] is True
    assert result["rows"] == []


async def test_every_cell_is_json_safe() -> None:
    engine = _Engine(
        columns=("n", "d", "ts", "t", "i", "u", "b", "arr", "j", "inf"),
        rows=[
            (
                Decimal("12.50"),
                date(2026, 9, 1),
                datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
                time(8, 15),
                timedelta(days=2),
                uuid.UUID(int=1),
                b"\x01\xff",
                [Decimal("1.5"), None],
                {"k": Decimal(2)},
                float("inf"),
            )
        ],
    )

    result = await _run(engine, "SELECT product FROM sales_v")

    encoded = json.dumps(result, allow_nan=False)
    row = result["rows"][0]
    assert row[0] == "12.50"
    assert row[1] == "2026-09-01"
    assert row[6] == "\\x01ff"
    assert row[7] == ["1.5", None]
    assert row[8] == {"k": "2"}
    assert isinstance(row[9], str)
    assert encoded


# ---------------------------------------------------------------------------
# Database errors: fixed texts, never the driver's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sqlstate,error_kind",
    [
        ("57014", "query_timeout"),
        ("55P03", "query_timeout"),
        ("42501", "refused_by_database"),
        ("25006", "refused_by_database"),
        ("42703", "query_invalid"),
        ("42601", "query_invalid"),
        ("22012", "data_error"),
        ("53200", "query_failed"),
        (None, "database_unavailable"),
    ],
)
async def test_database_errors_map_to_fixed_safe_texts(
    sqlstate: str | None, error_kind: str
) -> None:
    engine = _Engine(query_error=_db_error(sqlstate))

    result = await _run(engine, "SELECT product FROM sales_v")

    assert result["error_kind"] == error_kind
    dumped = json.dumps(result)
    for fragment in ("hunter2", "db.internal", "secret_table", "SELECT 1"):
        assert fragment not in dumped
    assert engine.rolled_back


async def test_the_timeout_text_states_the_configured_limit() -> None:
    result = await _run(
        _Engine(query_error=_db_error("57014")), "SELECT 1 FROM sales_v"
    )
    assert "1.5" in result["error"]


#: What connecting really raises. On the asyncpg connect path SQLAlchemy does
#: not wrap these: a closed port, an unknown host, a rejected password and a
#: connect timeout reach the caller as the driver's or the OS's own types.
_CONNECT_FAILURES = [
    ConnectionRefusedError(111, f"Connect call failed {_DRIVER_SECRET}"),
    socket.gaierror(-2, f"Name or service not known {_DRIVER_SECRET}"),
    asyncpg.exceptions.InvalidPasswordError(f"auth failed {_DRIVER_SECRET}"),
    asyncpg.exceptions.TooManyConnectionsError(f"too many {_DRIVER_SECRET}"),
    asyncpg.exceptions.ConnectionDoesNotExistError(f"closed {_DRIVER_SECRET}"),
    TimeoutError(f"connect timed out {_DRIVER_SECRET}"),
    OperationalError("connect", {}, Exception(_DRIVER_SECRET)),
]


@pytest.mark.parametrize("error", _CONNECT_FAILURES, ids=lambda e: type(e).__name__)
async def test_an_unreachable_database_is_a_result_not_an_exception(
    error: Exception,
) -> None:
    engine = _Engine(connect_error=error)

    result = await _run(engine, "SELECT product FROM sales_v")

    assert result["error_kind"] == "database_unavailable"
    assert "hunter2" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("byte_limit", [0, 1_023, 1_048_577])
def test_byte_limit_must_be_bounded(byte_limit: int) -> None:
    with pytest.raises(ValueError):
        SqlQueryConfig(views=_CONFIG.views, byte_limit=byte_limit)


def test_the_description_states_the_byte_budget() -> None:
    spec = build_sql_query_tool_spec(None, _CONFIG)
    assert f"{_CONFIG.byte_limit} bytes" in spec.description


def test_row_limit_cannot_exceed_the_hard_ceiling() -> None:
    with pytest.raises(ValueError):
        SqlQueryConfig(views={"reporting.sales_v": ""}, row_limit=10_000)


@pytest.mark.parametrize("timeout_ms", [0, -1, 120_000])
def test_statement_timeout_must_be_bounded(timeout_ms: int) -> None:
    with pytest.raises(ValueError):
        SqlQueryConfig(views={"reporting.sales_v": ""}, statement_timeout_ms=timeout_ms)


@pytest.mark.parametrize("views", [{}, {"sales_v": ""}, {"a.b.c": ""}])
def test_views_must_be_a_non_empty_set_of_schema_qualified_names(
    views: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        SqlQueryConfig(views=views)


# ---------------------------------------------------------------------------
# Tool surface: permission, tier, R2a/R2b, R4
# ---------------------------------------------------------------------------


def test_tool_spec_shape() -> None:
    spec = build_sql_query_tool_spec(object(), _CONFIG)

    assert spec.name == SQL_QUERY_TOOL_NAME == "sql_query"
    assert spec.required_permissions == (QUERY_SQL_PERMISSION,) == ("query:sql",)
    assert spec.tier is Tier.T2
    assert spec.input_schema["required"] == ["sql"]
    assert "reporting.sales_v" in spec.description
    assert "One row per invoice line" in spec.description


def test_query_sql_is_its_own_t2_permission_family() -> None:
    cls = permission_registry.resolve("query:sql")

    assert issubclass(cls, builtins.Query)
    assert cls is not builtins.Query
    assert cls.tier is Tier.T2
    assert builtins.Query.__mro__[1] is builtins.Permission
    assert not issubclass(cls, builtins.Read)


def test_the_tool_is_revalidated_at_call_time() -> None:
    assert _is_sensitive(build_sql_query_tool_spec(object(), _CONFIG))


async def _noop(inputs: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
    return {}


def test_r2a_a_lower_tier_tool_cannot_require_query_sql() -> None:
    with pytest.raises(PermissionTierMismatchError):
        ToolSpec(
            name="x",
            required_permissions=("query:sql",),
            connector=_noop,
            tier=Tier.T1,
        )


def test_r2b_a_t3_tool_cannot_rest_on_query_sql_alone() -> None:
    with pytest.raises(PermissionFloorViolationError):
        ToolSpec(
            name="x",
            required_permissions=("query:sql",),
            connector=_noop,
            tier=Tier.T3,
        )


def _write_role(root: pathlib.Path, name: str, permissions: list[str]) -> None:
    folder = root / "roles" / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f"---\nname: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    perms = "\n".join(f"  - {p}" for p in permissions)
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\ntools: []\nskills: []\ncontext: {{}}\n"
        f"permissions:\n{perms}\n---\n\nm\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\nuntrusted_input: true\n"
        "execution_limits: null\n---\n\np\n",
        encoding="utf-8",
    )


def test_r4_an_untrusted_input_role_may_declare_query_sql(
    tmp_path: pathlib.Path,
) -> None:
    # T2, not T3: the R4 barrier does not apply, so granting it to a role
    # that faces untrusted input is a deployment decision (see ADR-007).
    _write_role(tmp_path, "public-analyst", ["query:sql"])

    definition = resolve("public-analyst", roots=RootConfig(platform_root=tmp_path))

    assert "query:sql" in definition.permissions


def test_r4_still_bars_t3_from_the_same_role(tmp_path: pathlib.Path) -> None:
    _write_role(tmp_path, "public-analyst", ["query:sql", "exec:command"])

    with pytest.raises(UntrustedInputGrantError):
        resolve("public-analyst", roots=RootConfig(platform_root=tmp_path))
