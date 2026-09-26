"""Shared read-only-role verification.

Extracted from ``main._bi_role_is_read_only`` so more than one entrypoint can
ask the same question of a database role without duplicating the reasoning:
``main.py``'s lifespan asks it about ``BI_DATABASE_URL`` before binding
``run_report``, and ``demo.py``'s entrypoint asks it about
``DEMO_DATABASE_URL`` before serving the demo database at all.

The read-only SQL tool (#80, ADR-007) asks a stricter question of its own
role, because the model writes that tool's SQL: not only "is every
transaction read-only by default?" but "can this role write anything at all,
or read anything beyond its allowlisted views?" (`check_query_role`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


async def role_is_read_only(
    engine: Any, *, log_event: str = "db.read_only_check_failed"
) -> bool | None:
    """Ask the database whether the role behind *engine* really is read-only.

    Returns True / False, or None when the question could not be answered —
    those are three different situations and collapsing the third into either
    of the other two is the bug. "Could not determine" must not read as
    "determined to be writable" (that would take a dependent feature down
    whenever a reporting replica is briefly unreachable), and it must not
    read as "determined to be read-only" either (that would restore the very
    assumption this check exists to remove).

    A dedicated read-only role is a layer that is meant to hold even if
    parameter validation and any application-level interceptor both have
    bugs, and nothing else confirms it was configured — the connection URL
    is trusted to point at a role someone set up by hand.

    *log_event* lets each caller keep its own existing log event name (e.g.
    ``main.py`` uses ``"bi.read_only_check_failed"``) rather than adopting a
    shared one that would change observable behaviour for callers this
    function did not originate from.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW default_transaction_read_only"))
            return str(result.scalar()).strip().lower() == "on"
    except SQLAlchemyError:
        structlog.get_logger().warning(log_event, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The read-only SQL tool's role (#80, ADR-007)
# ---------------------------------------------------------------------------

_QUERY_ROLE_FACTS = text(
    """
    SELECT pg_catalog.current_setting('default_transaction_read_only')
               AS default_read_only,
           r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication,
           r.rolbypassrls
    FROM pg_catalog.pg_roles AS r
    WHERE r.rolname = current_user
    """
)
"""Role-level facts. `default_transaction_read_only` is read here, not
`transaction_read_only`: the connector sets its own transaction READ ONLY, so
only the role default tells whether the ROLE is read-only."""

_QUERY_ROLE_FINDINGS = text(
    r"""
    SELECT 'relation' AS finding, n.nspname AS schema_name,
           c.relname AS object_name, c.relkind::text AS relkind,
           pg_catalog.has_any_column_privilege(c.oid, 'SELECT') AS can_select,
           (pg_catalog.has_any_column_privilege(c.oid, 'INSERT, UPDATE, REFERENCES')
            OR pg_catalog.has_table_privilege(c.oid, 'DELETE, TRUNCATE, TRIGGER'))
               AS can_write
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_toast%'
      AND n.nspname NOT LIKE 'pg\_temp\_%'
      AND (pg_catalog.has_any_column_privilege(
               c.oid, 'SELECT, INSERT, UPDATE, REFERENCES')
           OR pg_catalog.has_table_privilege(c.oid, 'DELETE, TRUNCATE, TRIGGER'))
    UNION ALL
    SELECT 'sequence', n.nspname, c.relname, c.relkind::text, false, true
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE c.relkind = 'S'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND pg_catalog.has_sequence_privilege(c.oid, 'USAGE, UPDATE')
    UNION ALL
    SELECT 'schema', n.nspname, NULL, NULL, false, true
    FROM pg_catalog.pg_namespace AS n
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\_toast%'
      AND n.nspname NOT LIKE 'pg\_temp\_%'
      AND pg_catalog.has_schema_privilege(n.oid, 'CREATE')
    """
)
"""Everything outside the system schemas this role can read or change:
relations it can SELECT from or write to, sequences it can advance, and
schemas it can create objects in. Privileges granted to PUBLIC count, because
they reach this role too. Static text, no caller input (AD-2 holds here)."""

_ELEVATED_ATTRIBUTES = (
    "rolsuper",
    "rolcreaterole",
    "rolcreatedb",
    "rolreplication",
    "rolbypassrls",
)
_VIEW_RELKINDS = frozenset({"v", "m"})


@dataclass(frozen=True)
class QueryRoleCheck:
    """What `check_query_role` found.

    `problems` make the role unsafe for model-authored SQL and the tool
    refuses to run while any exist. `warnings` are configuration gaps that
    are not unsafe (an allowlisted view the role cannot read just fails that
    query at the database).
    """

    problems: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.problems


def evaluate_query_role(
    facts: Mapping[str, Any] | None,
    findings: Iterable[Mapping[str, Any]],
    allowed: frozenset[tuple[str, str]],
) -> QueryRoleCheck:
    """Decide whether the role behind *facts*/*findings* is safe for the tool.

    Safe means all of: the role's own default makes every transaction
    read-only; it has no elevated attribute; it can write no relation,
    sequence or schema; it can SELECT from no relation outside *allowed*; and
    every allowlisted relation it can read is a view or materialized view.
    """
    if facts is None:
        return QueryRoleCheck(problems=("the current role was not found",), warnings=())

    problems: list[str] = []
    warnings: list[str] = []
    if str(facts.get("default_read_only", "")).strip().lower() != "on":
        problems.append("default_transaction_read_only is not on for this role")
    elevated = [name for name in _ELEVATED_ATTRIBUTES if facts.get(name)]
    if elevated:
        problems.append(f"role has elevated attributes: {', '.join(elevated)}")

    readable: set[tuple[str, str]] = set()
    for finding in findings:
        kind = finding["finding"]
        schema = str(finding["schema_name"])
        name = finding.get("object_name")
        shown = schema if name is None else f"{schema}.{name}"
        if kind == "schema":
            problems.append(f"role can create objects in schema {shown}")
            continue
        if finding.get("can_write"):
            problems.append(f"role can write {kind} {shown}")
        if kind != "relation" or not finding.get("can_select"):
            continue
        key = (schema, str(name))
        if key not in allowed:
            problems.append(f"role can read {shown}, which is not allowlisted")
            continue
        readable.add(key)
        if finding.get("relkind") not in _VIEW_RELKINDS:
            problems.append(f"allowlisted relation {shown} is not a view")

    for schema, name in sorted(allowed - readable):
        warnings.append(f"role cannot read allowlisted view {schema}.{name}")
    return QueryRoleCheck(problems=tuple(problems), warnings=tuple(warnings))


async def check_query_role(
    conn: Any, allowed: frozenset[tuple[str, str]]
) -> QueryRoleCheck:
    """Ask the database, over *conn*, what the current role can do."""
    facts = (await conn.execute(_QUERY_ROLE_FACTS)).mappings().first()
    findings = (await conn.execute(_QUERY_ROLE_FINDINGS)).mappings().all()
    return evaluate_query_role(facts, findings, allowed)


async def verify_query_role(
    engine: Any,
    allowed: frozenset[tuple[str, str]],
    *,
    log_event: str = "sql_query.role_check_failed",
) -> bool | None:
    """Startup form of `check_query_role`, shaped like `role_is_read_only`.

    True: safe. False: unsafe, with every problem logged - a deployment
    should refuse to start (or refuse to bind the tool). None: the question
    could not be answered; that is neither "safe" nor "unsafe", and the tool
    itself re-checks on every call regardless.
    """
    logger = structlog.get_logger()
    try:
        async with engine.connect() as conn:
            try:
                check = await check_query_role(conn, allowed)
            finally:
                await conn.rollback()
    except SQLAlchemyError:
        logger.warning(log_event, exc_info=True)
        return None
    for warning in check.warnings:
        logger.warning("sql_query.role_warning", detail=warning)
    if check.problems:
        logger.error("sql_query.role_not_read_only", problems=list(check.problems))
        return False
    return True
