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
) -> dict[str, Any]:
    return {
        "finding": finding,
        "schema_name": schema,
        "object_name": name,
        "relkind": relkind,
        "can_select": can_select,
        "can_write": can_write,
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


def test_an_allowlisted_base_table_is_a_problem() -> None:
    findings = [_relation("sales_v", relkind="r"), _relation("clients_v")]

    check = evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED)

    assert not check.safe
    assert any("reporting.sales_v" in p for p in check.problems)


def test_a_materialized_view_is_an_acceptable_allowlisted_relation() -> None:
    findings = [_relation("sales_v", relkind="m"), _relation("clients_v")]
    assert evaluate_query_role(_SAFE_FACTS, findings, _ALLOWED).safe


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
