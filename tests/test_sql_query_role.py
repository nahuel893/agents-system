"""Offline tests for the read-only SQL tool's role verification (#80, ADR-007).

`check_query_role` asks the database what the tool's role can actually do;
`evaluate_query_role` turns those facts into problems (the tool refuses to
run) and warnings (logged, not blocking). The catalog SQL itself is exercised
against a real PostgreSQL in `tests/test_sql_query_integration.py`; here the
facts are canned so every decision rule is pinned without a database.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

import pytest
from sqlalchemy.exc import OperationalError

from agents_system.services.db_role import (
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
    def __init__(self, connection: _Connection | None) -> None:
        self._connection = connection

    def connect(self) -> _Connection:
        if self._connection is None:
            raise OperationalError("connect", {}, Exception("unreachable"))
        return self._connection


async def test_verify_query_role_is_true_for_a_safe_role() -> None:
    engine = _Engine(_Connection(_SAFE_FACTS, _SAFE_FINDINGS))
    assert await verify_query_role(engine, _ALLOWED) is True


async def test_verify_query_role_is_false_for_a_writable_role() -> None:
    engine = _Engine(_Connection(_facts(default_read_only="off"), _SAFE_FINDINGS))
    assert await verify_query_role(engine, _ALLOWED) is False


async def test_verify_query_role_is_none_when_the_database_cannot_answer() -> None:
    assert await verify_query_role(_Engine(None), _ALLOWED) is None
