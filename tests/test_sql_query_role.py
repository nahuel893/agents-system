"""Offline tests for the read-only SQL tool's role verification (#80, ADR-007).

`check_query_role` asks the database what the tool's role can actually do;
`evaluate_query_role` turns those facts into problems (the tool refuses to
run) and warnings (logged, not blocking). The catalog SQL itself is exercised
against a real PostgreSQL in `tests/test_sql_query_integration.py`; here the
facts are canned so every decision rule is pinned without a database.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from typing import Any, Self

import asyncpg
import pytest
from sqlalchemy.exc import OperationalError

from agents_system.services.db_role import (
    MAX_TEMP_FILE_LIMIT_KB,
    evaluate_query_role,
    role_is_read_only,
    verify_query_role,
)

_ALLOWED = frozenset({("reporting", "sales_v"), ("reporting", "clients_v")})

_SAFE_FACTS: dict[str, Any] = {
    "default_read_only": "on",
    "rolsuper": False,
    "rolcreaterole": False,
    "rolcreatedb": False,
    "rolreplication": False,
    "rolbypassrls": False,
    "member_of": [],
    "can_create_schemas": False,
    "temp_file_limit_kb": 262_144,
}


def _relation(
    name: str,
    *,
    schema: str = "reporting",
    relkind: str = "v",
    can_select: bool = True,
    can_write: bool = False,
    finding: str = "relation",
    security_barrier: bool | None = None,
) -> dict[str, Any]:
    # The catalog query reports security_barrier for plain views only.
    if security_barrier is None and relkind == "v":
        security_barrier = True
    return {
        "finding": finding,
        "schema_name": schema,
        "object_name": name,
        "relkind": relkind,
        "can_select": can_select,
        "can_write": can_write,
        "security_barrier": security_barrier,
    }


_SAFE_FINDINGS = [_relation("sales_v"), _relation("clients_v")]


def _facts(**overrides: Any) -> dict[str, Any]:
    return {**_SAFE_FACTS, **overrides}


def test_a_role_that_reads_exactly_the_allowlist_has_no_problems() -> None:
    check = evaluate_query_role(_SAFE_FACTS, _SAFE_FINDINGS, _ALLOWED)

    assert check.safe
    assert check.problems == ()
    assert check.warnings == ()


def test_a_missing_role_row_is_a_problem() -> None:
    assert not evaluate_query_role(None, _SAFE_FINDINGS, _ALLOWED).safe


def test_default_transaction_read_only_off_is_a_problem() -> None:
    check = evaluate_query_role(
        _facts(default_read_only="off"), _SAFE_FINDINGS, _ALLOWED
    )

    assert not check.safe
    assert any("default_transaction_read_only" in p for p in check.problems)


@pytest.mark.parametrize(
    "attribute",
    ["rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls"],
)
def test_elevated_role_attributes_are_problems(attribute: str) -> None:
    check = evaluate_query_role(_facts(**{attribute: True}), _SAFE_FINDINGS, _ALLOWED)
    assert not check.safe


@pytest.mark.parametrize("limit_kb", [-1, None])
def test_unlimited_temporary_files_are_a_problem(limit_kb: int | None) -> None:
    # -1 is PostgreSQL's default: no cap on the temporary files a session
    # may write. A sort or hash of huge values can then fill the volume that
    # also holds the data files and the WAL.
    check = evaluate_query_role(
        _facts(temp_file_limit_kb=limit_kb), _SAFE_FINDINGS, _ALLOWED
    )

    assert not check.safe
    assert any("temp_file_limit" in p for p in check.problems)


def test_a_temporary_file_limit_above_the_ceiling_is_a_problem() -> None:
    check = evaluate_query_role(
        _facts(temp_file_limit_kb=MAX_TEMP_FILE_LIMIT_KB + 1), _SAFE_FINDINGS, _ALLOWED
    )

    assert not check.safe


@pytest.mark.parametrize("limit_kb", [0, 1_024, MAX_TEMP_FILE_LIMIT_KB])
def test_a_bounded_temporary_file_limit_is_safe(limit_kb: int) -> None:
    check = evaluate_query_role(
        _facts(temp_file_limit_kb=limit_kb), _SAFE_FINDINGS, _ALLOWED
    )

    assert check.safe


def test_any_write_privilege_is_a_problem() -> None:
    findings = [*_SAFE_FINDINGS[:1], _relation("clients_v", can_write=True)]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any("reporting.clients_v" in p for p in check.problems)


def test_reading_a_relation_outside_the_allowlist_is_a_problem() -> None:
    findings = [*_SAFE_FINDINGS, _relation("orders", schema="public", relkind="r")]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any("public.orders" in p for p in check.problems)


@pytest.mark.parametrize("relkind", ["r", "p"])
def test_an_allowlisted_base_table_is_a_problem_by_default(relkind: str) -> None:
    # The default keeps the check's original strictness: a table in the
    # allowlist (or a stray SELECT grant on one) stops the tool unless the
    # deployment opted in to base tables.
    findings = [_relation("sales_v", relkind=relkind), _relation("clients_v")]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any(
        "reporting.sales_v" in p and "allow_base_tables" in p for p in check.problems
    )


@pytest.mark.parametrize("relkind", ["r", "p"])
def test_an_allowlisted_base_table_is_safe_when_the_deployment_opts_in(
    relkind: str,
) -> None:
    # A table hides no rows, so there is nothing for a security barrier to
    # order the model's predicates after: the role's privileges are the
    # whole boundary, and they are checked exactly as for a view.
    findings = [_relation("sales_v", relkind=relkind), _relation("clients_v")]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED, allow_base_tables=True)

    assert check.safe
    assert check.problems == ()


@pytest.mark.parametrize("relkind", ["r", "p"])
def test_an_allowlisted_base_table_is_still_refused_when_it_can_be_written(
    relkind: str,
) -> None:
    findings = [
        _relation("sales_v", relkind=relkind, can_write=True),
        _relation("clients_v"),
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED, allow_base_tables=True)

    assert not check.safe
    assert any("can write" in p and "reporting.sales_v" in p for p in check.problems)


@pytest.mark.parametrize("relkind", ["r", "p"])
def test_a_base_table_outside_the_allowlist_is_a_problem_in_both_modes(
    relkind: str,
) -> None:
    findings = [*_SAFE_FINDINGS, _relation("orders", schema="public", relkind=relkind)]

    for allow in (False, True):
        check = evaluate_query_role(
            _SAFE_FACTS, findings, _ALLOWED, allow_base_tables=allow
        )

        assert not check.safe
        assert any(
            "public.orders" in p and "not allowlisted" in p for p in check.problems
        )


@pytest.mark.parametrize("barrier", [False, None])
def test_the_opt_in_does_not_relax_the_barrier_rule_for_plain_views(
    barrier: bool | None,
) -> None:
    findings = [
        {**_relation("sales_v"), "security_barrier": barrier},
        _relation("clients_v"),
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED, allow_base_tables=True)

    assert not check.safe
    assert any(
        "security_barrier" in p and "reporting.sales_v" in p for p in check.problems
    )


def test_a_foreign_table_is_refused_even_with_the_opt_in() -> None:
    # Remote data, reached with credentials and remote privileges this
    # role's own grants say nothing about.
    findings = [_relation("sales_v", relkind="f"), _relation("clients_v")]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED, allow_base_tables=True)

    assert not check.safe
    assert any("reporting.sales_v" in p for p in check.problems)


def test_a_mix_of_views_and_base_tables_is_safe_with_the_opt_in() -> None:
    findings = [
        _relation("sales_v", relkind="r"),
        _relation("clients_v", relkind="m"),
    ]

    assert evaluate_query_role(
        _SAFE_FACTS, findings, _ALLOWED, allow_base_tables=True
    ).safe


def test_a_materialized_view_is_an_acceptable_allowlisted_relation() -> None:
    # It holds its own rows: no filter of the view runs at query time, so
    # there is nothing for the model's predicates to be pushed below.
    findings = [_relation("sales_v", relkind="m"), _relation("clients_v")]
    assert evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED).safe


@pytest.mark.parametrize("barrier", [False, None])
def test_an_allowlisted_view_without_security_barrier_is_a_problem(
    barrier: bool | None,
) -> None:
    # Without security_barrier the planner may evaluate the model's predicate
    # below the view's own row filter, on rows the view hides: an error
    # raised only for some hidden values (a division by zero) tells the
    # model what they are, one call at a time.
    findings = [
        {**_relation("sales_v"), "security_barrier": barrier},
        _relation("clients_v"),
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any(
        "security_barrier" in p and "reporting.sales_v" in p for p in check.problems
    )


def test_a_writable_sequence_is_a_problem() -> None:
    findings = [
        *_SAFE_FINDINGS,
        _relation(
            "orders_id_seq",
            relkind="S",
            can_select=False,
            can_write=True,
            finding="sequence",
        ),
    ]
    assert not evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED).safe


def test_a_readable_sequence_is_a_problem() -> None:
    # `can_select` for a sequence used to be hardcoded false, so a role
    # granted plain SELECT on a sequence (never allowlisted - the allowlist
    # only ever holds views) still verified as safe, letting the tool read
    # the sequence's last_value (e.g. leaking a row count) (issue #131).
    findings = [
        *_SAFE_FINDINGS,
        _relation(
            "orders_id_seq",
            relkind="S",
            can_select=True,
            can_write=False,
            finding="sequence",
        ),
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any("orders_id_seq" in p and "not allowlisted" in p for p in check.problems)


def test_create_on_a_schema_is_a_problem() -> None:
    findings = [
        *_SAFE_FINDINGS,
        {
            "finding": "schema",
            "schema_name": "public",
            "object_name": None,
            "relkind": None,
            "can_select": False,
            "can_write": True,
        },
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any("public" in p for p in check.problems)


@pytest.mark.parametrize(
    "roles",
    [
        ["pg_execute_server_program", "pg_read_server_files"],
        ["pg_signal_backend"],
        ["reporting_owner"],
    ],
)
def test_membership_in_any_other_role_is_a_problem(roles: list[str]) -> None:
    # A predefined role grants server-side capabilities (running programs,
    # reading files, signalling backends) that neither a READ ONLY
    # transaction nor relation privileges contain; any other role brings
    # its own privileges. The provisioning script strips memberships, and
    # this check notices when one comes back.
    check = evaluate_query_role(_facts(member_of=roles), _SAFE_FINDINGS, _ALLOWED)

    assert not check.safe
    assert any(all(role in p for role in roles) for p in check.problems)


def test_create_on_the_database_is_a_problem() -> None:
    check = evaluate_query_role(
        _facts(can_create_schemas=True), _SAFE_FINDINGS, _ALLOWED
    )

    assert not check.safe
    assert any("create schemas" in p for p in check.problems)


def test_an_executable_security_definer_function_is_a_problem() -> None:
    # It runs with its owner's privileges, so it can read what this role
    # cannot - a qualified call to it is how the database layer alone would
    # be bypassed.
    findings = [
        *_SAFE_FINDINGS,
        {
            "finding": "function",
            "schema_name": "reporting",
            "object_name": "lower",
            "relkind": None,
            "can_select": False,
            "can_write": False,
        },
    ]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any(
        "SECURITY DEFINER" in p and "reporting.lower" in p for p in check.problems
    )


def test_an_allowlisted_view_the_role_cannot_read_is_only_a_warning() -> None:
    check = evaluate_query_role(_SAFE_FACTS, _SAFE_FINDINGS[:1], _ALLOWED)

    assert check.safe
    assert any("reporting.clients_v" in w for w in check.warnings)


# ---------------------------------------------------------------------------
# verify_query_role: True / False / None, like role_is_read_only
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Mapping[str, Any]]:
        return list(self._rows)


class _Connection:
    def __init__(self, facts: Mapping[str, Any], findings: list[Any]) -> None:
        self._facts = facts
        self._findings = findings

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, clause: Any, params: Any = None) -> _Result:
        if "pg_roles" in str(clause):
            return _Result([self._facts])
        return _Result(self._findings)

    async def rollback(self) -> None:
        return None


class _Engine:
    def __init__(
        self, connection: _Connection | None, error: Exception | None = None
    ) -> None:
        self._connection = connection
        self._error = error or OperationalError("connect", {}, Exception("unreachable"))

    def connect(self) -> _Connection:
        if self._connection is None:
            raise self._error
        return self._connection


async def test_verify_query_role_is_true_for_a_safe_role() -> None:
    engine = _Engine(_Connection(_SAFE_FACTS, _SAFE_FINDINGS))
    assert await verify_query_role(engine, _ALLOWED) is True


async def test_verify_query_role_passes_the_base_table_opt_in_through() -> None:
    findings = [_relation("sales_v", relkind="r"), _relation("clients_v")]
    engine = _Engine(_Connection(_SAFE_FACTS, findings))

    assert await verify_query_role(engine, _ALLOWED) is False
    assert await verify_query_role(engine, _ALLOWED, allow_base_tables=True) is True


async def test_verify_query_role_is_false_for_a_writable_role() -> None:
    engine = _Engine(_Connection(_facts(default_read_only="off"), _SAFE_FINDINGS))
    assert await verify_query_role(engine, _ALLOWED) is False


async def test_verify_query_role_is_none_when_the_database_cannot_answer() -> None:
    assert await verify_query_role(_Engine(None), _ALLOWED) is None


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(111, "Connect call failed"),
        socket.gaierror(-2, "Name or service not known"),
        asyncpg.exceptions.InvalidPasswordError("password authentication failed"),
        TimeoutError("connect timed out"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_verify_query_role_is_none_for_the_real_connect_failures(
    error: Exception,
) -> None:
    # SQLAlchemy does not wrap these on the asyncpg connect path; "could
    # not answer" must still be None, not an exception at startup.
    assert await verify_query_role(_Engine(None, error), _ALLOWED) is None


# ---------------------------------------------------------------------------
# role_is_read_only: the sibling check `verify_query_role` is modeled on
# (issue #131) - it caught only SQLAlchemyError, so the same raw asyncpg/OS
# connect failures that `verify_query_role` already tolerates escaped here
# instead and aborted whichever caller was checking a BI/demo database.
# ---------------------------------------------------------------------------


class _ShowResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar(self) -> str:
        return self._value


class _ShowConnection:
    def __init__(self, value: str) -> None:
        self._value = value

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, clause: Any) -> _ShowResult:
        return _ShowResult(self._value)


class _ShowEngine:
    def __init__(
        self, value: str | None = None, error: Exception | None = None
    ) -> None:
        self._value = value
        self._error = error

    def connect(self) -> _ShowConnection:
        if self._error is not None:
            raise self._error
        assert self._value is not None
        return _ShowConnection(self._value)


async def test_role_is_read_only_is_true_when_the_default_is_on() -> None:
    assert await role_is_read_only(_ShowEngine("on")) is True


async def test_role_is_read_only_is_false_when_the_default_is_off() -> None:
    assert await role_is_read_only(_ShowEngine("off")) is False


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(111, "Connect call failed"),
        socket.gaierror(-2, "Name or service not known"),
        asyncpg.exceptions.InvalidPasswordError("password authentication failed"),
        TimeoutError("connect timed out"),
        OperationalError("connect", {}, Exception("unreachable")),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_role_is_read_only_is_none_for_every_real_connect_failure(
    error: Exception,
) -> None:
    # Only SQLAlchemyError used to be caught: on the asyncpg connect path
    # SQLAlchemy does not wrap a closed port, an unknown host, a rejected
    # password or a connect timeout, so each of these used to escape and
    # abort startup instead of answering "could not determine".
    assert await role_is_read_only(_ShowEngine(error=error)) is None
