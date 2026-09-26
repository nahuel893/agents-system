"""Application-layer guard for model-authored SQL (#80, ADR-007).

ADR-007 amends AD-2 for exactly one tool: `sql_query` accepts SQL text written
by the model. The trust boundary for that text is the DATABASE - a dedicated
read-only role that can only SELECT from an allowlist of views, inside a READ
ONLY transaction with a server-side statement timeout (see
`connectors/sql_query_connector.py` and `scripts/provision_sql_readonly.sql`).
This module is the second layer, and it is built to hold on its own even if
the role were misconfigured:

- The text is parsed by a real PostgreSQL-dialect parser (sqlglot), never
  string-matched. Exactly one statement; its root must be a plain SELECT or a
  set operation of SELECTs. Anything that writes or locks - DML/DDL anywhere
  in the tree, data-modifying CTEs, `SELECT ... INTO`, `FOR UPDATE/SHARE` -
  is refused.
- Every relation must resolve, by PostgreSQL's own identifier rules
  (unquoted folds to lower case, quoted is exact, CTE names shadow only
  inside their scope), to an allowlisted `schema.view`.
- Every function call must be an unqualified name on a function allowlist,
  read from the canonical rendering itself (the name PostgreSQL receives);
  qualified calls (in FROM too), custom operators, bind parameters, casts to
  non-built-in types and string literals other than plain '...' are refused.
- The guard's own cost is bounded: the text is capped at `max_sql_length`
  characters and the parsed tree at `max_nodes` nodes, and every check is a
  linear pass (function names are checked during the one render of the
  statement, never by rendering each node on its own).
- What reaches the database is NOT the model's text but the canonical
  rendering of the validated tree: every identifier quoted exactly as it was
  resolved, every relation schema-qualified, comments dropped, and the whole
  statement wrapped as `SELECT * FROM (...) LIMIT n + 1`. That rendering is
  parsed and validated a second time and must render back to the identical
  text, and it stays inside a lexical subset this parser and PostgreSQL agree
  on (quoted identifiers, plain strings with doubled quotes, no comments). A
  parser differential - text read one way here and another way by
  PostgreSQL - therefore cannot smuggle anything past the checks.

Rejections carry a fixed reason `code` and a fixed `message`. A message may
name the deployment's own configuration (the queryable views) or an
identifier taken from the validated tree, truncated to PostgreSQL's 63-byte
identifier limit; it never carries database or driver output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.generators.postgres import PostgresGenerator
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

_DIALECT = "postgres"
_PG_IDENTIFIER_MAX = 63
_SIMPLE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_CALL_NAME = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_RESULT_ALIAS = "sql_query_result"

DEFAULT_MAX_SQL_LENGTH = 10_000
"""Longest query text the guard parses. A real analytical question fits in a
fraction of this; anything longer is refused before the parser sees it."""

DEFAULT_MAX_NODES = 2_500
"""Most syntax-tree nodes the guard validates. A real analytical query has a
few hundred at most; the budget bounds the guard's own work (every check is
linear in the tree) independently of how the text packs its nodes."""

DEFAULT_ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Aggregates.
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "string_agg",
        "array_agg",
        "json_agg",
        "jsonb_agg",
        "bool_and",
        "bool_or",
        "every",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "corr",
        "covar_pop",
        "covar_samp",
        "percentile_cont",
        "percentile_disc",
        "mode",
        # Window functions.
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        # Conditionals.
        "coalesce",
        "nullif",
        "greatest",
        "least",
        # Math.
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "trunc",
        "sign",
        "power",
        "sqrt",
        "exp",
        "ln",
        "log",
        "div",
        "width_bucket",
        # Strings.
        "length",
        "char_length",
        "lower",
        "upper",
        "initcap",
        "trim",
        "ltrim",
        "rtrim",
        "btrim",
        "substring",
        "substr",
        "left",
        "right",
        "lpad",
        "rpad",
        "replace",
        "reverse",
        "concat",
        "concat_ws",
        "split_part",
        "position",
        "strpos",
        "regexp_replace",
        "overlay",
        "format",
        "to_char",
        "to_number",
        "starts_with",
        # Dates and times.
        "date_trunc",
        "date_part",
        "extract",
        "date_bin",
        "age",
        "make_date",
        "make_timestamp",
        "make_interval",
        "justify_days",
        "justify_hours",
        "justify_interval",
        "to_date",
        "to_timestamp",
        "date",
        "timezone",
        "isfinite",
        # Casts.
        "cast",
        # JSON.
        "json_build_object",
        "jsonb_build_object",
        "json_array_length",
        "jsonb_array_length",
        "json_extract_path_text",
        "jsonb_extract_path_text",
        "to_json",
        "to_jsonb",
        # Arrays and set-returning helpers.
        "array_length",
        "cardinality",
        "array_to_string",
        "string_to_array",
        "unnest",
        "generate_series",
    }
)
"""Functions a query may call, by their plain PostgreSQL name.

Every entry is side-effect free: it reads its arguments and nothing else. No
entry sleeps, takes a lock, reads or changes a setting, touches the
filesystem or a large object, advances a sequence, or runs SQL held in a
string (`query_to_xml` and friends would bypass the relation allowlist). A
deployment may pass a larger set; it then owns the review of what it adds.
"""

# Node types that never belong in a read-only query, wherever they appear.
# The root check already refuses them as whole statements; this catches them
# nested (a data-modifying CTE is a DELETE inside a SELECT's WITH clause).
_WRITE_OR_CONTROL_NODES: tuple[type[exp.Expr], ...] = (
    exp.DML,
    exp.DDL,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Revoke,
    exp.Command,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Analyze,
    exp.Execute,
    exp.Declare,
    exp.Show,
    exp.Comment,
    exp.Describe,
    exp.Use,
    exp.Pragma,
    exp.Kill,
    exp.LoadData,
    exp.Cache,
    exp.Uncache,
    exp.Refresh,
    exp.Summarize,
    # SELECT ... INTO creates a table; FOR UPDATE/SHARE takes row locks.
    exp.Into,
    exp.Lock,
)

# Typed nodes that are SQL syntax rather than function calls, even though
# they render as `KEYWORD(...)`: `EXISTS (subquery)`, `ARRAY(subquery)`.
_SYNTAX_NODES: tuple[type[exp.Expr], ...] = (exp.Exists, exp.Array)

# String literal forms other than plain '...'. Their escaping rules differ
# from a standard string's, and the renderer does not re-escape all of them:
# E'\\' (one backslash) comes back as e'\', where PostgreSQL reads `\'` as
# an escaped quote and the string swallows the rest of the statement. Plain
# strings plus the pinned `standard_conforming_strings` leave one escaping
# rule, doubled quotes, which the renderer and PostgreSQL agree on.
_NON_STANDARD_LITERALS: tuple[type[exp.Expr], ...] = (
    exp.ByteString,
    exp.UnicodeString,
    exp.BitString,
    exp.HexString,
    exp.National,
    exp.RawString,
)

_MESSAGES = {
    "invalid_input": (
        "The `sql` input must be a non-empty string holding one SELECT statement."
    ),
    "unparseable": (
        "The query could not be parsed as PostgreSQL. Send one plain SELECT "
        "(or WITH ... SELECT) statement."
    ),
    "multiple_statements": (
        "Exactly one statement is allowed per call, and the query contains "
        "more than one."
    ),
    "not_select": (
        "Only a read-only SELECT (or WITH ... SELECT) query is allowed. "
        "Writes, DDL, transaction control, SET, COPY, EXPLAIN and every other "
        "statement type are refused."
    ),
    "forbidden_clause": (
        "SELECT ... INTO, locking clauses (FOR UPDATE / FOR SHARE) and "
        "data-modifying WITH queries are not allowed: this tool only reads."
    ),
    "parameter_not_allowed": (
        "Bind parameters ($1, :name, ?) are not supported; write the values "
        "into the query as literals."
    ),
    "type_not_allowed": (
        "Casts are limited to built-in data types (for example numeric, text, "
        "date, timestamp, interval)."
    ),
    "unsupported_literal": (
        "Only plain '...' string literals are supported (double a quote to "
        "include one). Escape strings (E'...'), dollar-quoted strings "
        "($$...$$), Unicode-escape strings (U&'...'), and bit or hex strings "
        "are not."
    ),
}


class QueryRejectedError(ValueError):
    """The guard refused a query before it reached the database.

    `code` is a stable reason code for logs and callers; `message` is a
    fixed, model-safe explanation (see the module docstring).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject(code: str, message: str | None = None) -> QueryRejectedError:
    return QueryRejectedError(code, message or _MESSAGES[code])


def parse_relation_name(name: str) -> tuple[str, str]:
    """Parse one allowlist entry, `schema.view`, into its two parts.

    Allowlist entries are deployment configuration, not model input, and are
    held to the narrowest form that is unambiguous under PostgreSQL's
    identifier rules: exactly two simple, lower-case identifiers. A view that
    needs quoting to be named is out of scope rather than half supported.
    """
    parts = name.split(".")
    if len(parts) != 2 or not all(
        _SIMPLE_IDENTIFIER.fullmatch(part) and len(part) <= _PG_IDENTIFIER_MAX
        for part in parts
    ):
        raise ValueError(
            f"Allowlisted relation {name!r} must be written as schema.view, "
            "each part a simple lower-case PostgreSQL identifier."
        )
    return parts[0], parts[1]


@dataclass(frozen=True)
class QueryPolicy:
    """What a guarded query may touch: relations, functions and text length."""

    allowed_relations: frozenset[tuple[str, str]]
    allowed_functions: frozenset[str] = field(default=DEFAULT_ALLOWED_FUNCTIONS)
    max_sql_length: int = DEFAULT_MAX_SQL_LENGTH
    max_nodes: int = DEFAULT_MAX_NODES

    def __post_init__(self) -> None:
        if not self.allowed_relations:
            raise ValueError("QueryPolicy needs at least one allowlisted relation.")
        for schema, name in self.allowed_relations:
            parse_relation_name(f"{schema}.{name}")
        for function in self.allowed_functions:
            if not _SIMPLE_IDENTIFIER.fullmatch(function):
                raise ValueError(
                    f"Allowlisted function {function!r} must be a plain "
                    "lower-case name."
                )
        if self.max_sql_length < 1:
            raise ValueError("max_sql_length must be positive.")
        if self.max_nodes < 1:
            raise ValueError("max_nodes must be positive.")

    def relation_names(self) -> list[str]:
        return sorted(f"{schema}.{name}" for schema, name in self.allowed_relations)


@dataclass(frozen=True)
class GuardedQuery:
    """A validated query, ready to execute.

    `sql` is the canonical rendering of the validated tree, wrapped with the
    row limit; it is the only text the connector sends. `relations` lists the
    allowlisted `schema.view` names the query reads.
    """

    sql: str
    relations: tuple[str, ...]


def _shown(identifier: str) -> str:
    return identifier[:_PG_IDENTIFIER_MAX]


def _parse_single_statement(sql: str) -> exp.Expr:
    try:
        parsed = sqlglot.parse(sql, read=_DIALECT)
    except Exception as error:  # any parser failure is a refusal
        raise _reject("unparseable") from error
    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise _reject("invalid_input")
    if len(statements) > 1:
        raise _reject("multiple_statements")
    return statements[0]


def _too_complex(policy: QueryPolicy) -> QueryRejectedError:
    return _reject(
        "too_complex",
        f"The query is too complex for this tool (more than "
        f"{policy.max_nodes} syntax elements). Simplify it: fewer "
        "conditions or expressions, or aggregate in fewer steps.",
    )


def _check_size(statement: exp.Expr, policy: QueryPolicy, max_nodes: int) -> None:
    for count, _ in enumerate(statement.walk(), start=1):
        if count > max_nodes:
            raise _too_complex(policy)


def _check_nodes(statement: exp.Expr) -> None:
    """Structural checks: one pass over the tree, nothing rendered."""
    for node in statement.walk():
        if isinstance(node, _WRITE_OR_CONTROL_NODES):
            raise _reject("forbidden_clause")
        if isinstance(node, exp.CTE | exp.Subquery) and not isinstance(
            node.this, exp.Select | exp.SetOperation
        ):
            raise _reject("forbidden_clause")
        if isinstance(node, exp.Parameter | exp.Placeholder):
            raise _reject("parameter_not_allowed")
        if isinstance(node, _NON_STANDARD_LITERALS):
            raise _reject("unsupported_literal")
        if isinstance(node, exp.Operator):
            raise _reject(
                "function_not_allowed",
                "Explicit OPERATOR(...) calls are not allowed; use the plain "
                "built-in operators.",
            )
        if (
            isinstance(node, exp.DataType)
            and node.this == exp.DataType.Type.USERDEFINED
        ):
            raise _reject("type_not_allowed")
        # `schema.fn(...)` escapes the pinned search path and can reach a
        # deployment-owned function even when the bare name is allowlisted.
        # In a select list the qualifier is a Dot parent; a table function in
        # FROM carries it on its Table parent instead. Function NAMES are
        # checked as they are rendered (`_CheckedRenderer`).
        if isinstance(node, exp.Func) and (
            isinstance(node.parent, exp.Dot) or _is_qualified_table(node.parent)
        ):
            raise _qualified_function_rejected()


def _check_type(rendered: str) -> None:
    # reg* types (regclass, regproc, ...) look objects up in the catalogs.
    if rendered.upper().startswith("REG"):
        raise _reject("type_not_allowed")


def _function_not_allowed(name: str) -> QueryRejectedError:
    return _reject(
        "function_not_allowed",
        f"The query calls a function that is not allowed here: {_shown(name)}. "
        "Use standard aggregate, window, date, string and math functions, "
        "called by their plain name.",
    )


def _qualified_function_rejected() -> QueryRejectedError:
    return _reject(
        "function_not_allowed",
        "Schema-qualified function calls are not allowed; call built-in "
        "functions by their plain name.",
    )


def _is_qualified_table(node: exp.Expr | None) -> bool:
    return isinstance(node, exp.Table) and bool(
        node.args.get("db") or node.args.get("catalog")
    )


def _check_function(node: exp.Func, rendered: str, policy: QueryPolicy) -> None:
    """Check the name *node* reaches the database with, read from *rendered*."""
    if isinstance(node, _SYNTAX_NODES):
        return
    match = _CALL_NAME.match(rendered)
    if match is None:
        if isinstance(node, exp.Anonymous | exp.AnonymousAggFunc):
            # A quoted or otherwise unusual name: not what the allowlist holds.
            raise _function_not_allowed(rendered.split("(", 1)[0])
        # A typed node rendered as a keyword or operator form (CASE,
        # CURRENT_DATE, `->`): no function name reaches the database.
        return
    if match.group(1).lower() not in policy.allowed_functions:
        raise _function_not_allowed(match.group(1).lower())


class _CheckedRenderer(PostgresGenerator):
    """The canonical renderer, checking every function and type as it writes it.

    A function's name is read from its own rendering, so the name checked is
    the name PostgreSQL receives, whatever sqlglot parsed it into. Doing that
    inside the single render of the statement keeps the guard linear:
    rendering each node on its own re-rendered its whole subtree, and since
    AND/OR are function nodes, a long boolean chain made the guard quadratic.
    """

    def __init__(self, policy: QueryPolicy) -> None:
        super().__init__(dialect=_DIALECT, identify=True, comments=False)
        self._policy = policy
        self._checked: set[int] = set()
        self._tree: exp.Expr | None = None

    def preprocess(self, expression: exp.Expr) -> exp.Expr:
        self._tree = super().preprocess(expression)
        return self._tree

    def sql(
        self,
        expression: str | exp.Expr | None,
        key: str | None = None,
        comment: bool = True,
    ) -> str:
        rendered = super().sql(expression, key, comment)
        if key is None and isinstance(expression, exp.Func | exp.DataType):
            self._checked.add(id(expression))
            if isinstance(expression, exp.DataType):
                _check_type(rendered)
            else:
                _check_function(expression, rendered, self._policy)
        return rendered

    def render(self, statement: exp.Expr) -> str:
        text = self.generate(statement, copy=True)
        if self._tree is None:  # preprocess() did not run: nothing was checked
            raise _reject("unparseable")
        # Fail closed: a function or type the render did not pass through
        # `sql()` was not checked. The only exceptions are written as syntax,
        # never as a name followed by `(`: the inner links of an AND/OR or
        # same-operator chain (written iteratively as the operator itself)
        # and the WHEN branches of a CASE (written by the CASE as
        # `WHEN ... THEN ...`; their operands still pass through `sql()`).
        for node in self._tree.walk():
            if (
                isinstance(node, exp.Func | exp.DataType)
                and id(node) not in self._checked
                and not isinstance(node, exp.Connector | exp.Binary)
                and not _is_case_branch(node)
            ):
                raise _reject("unparseable")
        return text


def _is_case_branch(node: exp.Expr) -> bool:
    return (
        isinstance(node, exp.If)
        and isinstance(node.parent, exp.Case)
        and node.arg_key == "ifs"
    )


def _relation_not_allowed(shown: str, policy: QueryPolicy) -> QueryRejectedError:
    return _reject(
        "relation_not_allowed",
        f"The query reads a relation that is not on this deployment's "
        f"allowlist: {shown}. Queryable views: {', '.join(policy.relation_names())}.",
    )


def _resolve_relation(table: exp.Table, policy: QueryPolicy) -> str:
    """Check *table* against the allowlist and schema-qualify it in place."""
    name = table.name
    schema = table.db
    if table.catalog:
        shown = f"{_shown(table.catalog)}.{_shown(schema)}.{_shown(name)}"
        raise _relation_not_allowed(shown, policy)
    if schema:
        if (schema, name) not in policy.allowed_relations:
            raise _relation_not_allowed(f"{_shown(schema)}.{_shown(name)}", policy)
        return f"{schema}.{name}"
    candidates = sorted(s for s, n in policy.allowed_relations if n == name)
    if not candidates:
        raise _relation_not_allowed(_shown(name), policy)
    if len(candidates) > 1:
        raise _reject(
            "ambiguous_relation",
            f"The view name {_shown(name)} exists in more than one queryable "
            f"schema; qualify it: {', '.join(f'{s}.{name}' for s in candidates)}.",
        )
    table.set("db", exp.to_identifier(candidates[0]))
    return f"{candidates[0]}.{name}"


def _check_relations(statement: exp.Expr, policy: QueryPolicy) -> list[str]:
    try:
        scopes = traverse_scope(statement)
    except Exception as error:  # an unscopable tree is not a plain SELECT
        raise _reject("unparseable") from error

    checked: set[int] = set()
    relations: list[str] = []
    for scope in scopes:
        for table in scope.tables:
            if id(table) in checked:
                continue
            checked.add(id(table))
            if isinstance(table.this, exp.Func):
                # A table function (generate_series, unnest): its call is
                # held to the function allowlist, not the relation one, and
                # only by its plain name.
                if _is_qualified_table(table):
                    raise _qualified_function_rejected()
                continue
            if not isinstance(table.this, exp.Identifier):
                raise _reject("unparseable")
            if (
                not table.args.get("db")
                and not table.args.get("catalog")
                and table.name in scope.cte_sources
            ):
                continue  # a CTE visible in this scope, not a relation
            relations.append(_resolve_relation(table, policy))

    # Fail closed: a relation the scope walk did not visit was not checked.
    for table in statement.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) and id(table) not in checked:
            raise _reject("unparseable")
    return relations


def _validate(
    sql: str, policy: QueryPolicy, *, max_nodes: int
) -> tuple[exp.Select | exp.SetOperation, list[str]]:
    """Parse *sql* and run the structural and relation checks.

    Relations come back schema-qualified. Function names and types are
    checked by `_render`, which every accepted query goes through.
    """
    parsed = _parse_single_statement(sql)
    _check_size(parsed, policy, max_nodes)
    statement = normalize_identifiers(parsed, dialect=_DIALECT)
    if not isinstance(statement, exp.Select | exp.SetOperation):
        raise _reject("not_select")
    _check_nodes(statement)
    return statement, _check_relations(statement, policy)


def _render(statement: exp.Expr, policy: QueryPolicy) -> str:
    """Canonical text: every identifier quoted as resolved, no comments.

    Every function name and type is checked against *policy* as it is
    written (see `_CheckedRenderer`).
    """
    return _CheckedRenderer(policy).render(statement)


def guard_query(sql: object, policy: QueryPolicy, *, row_limit: int) -> GuardedQuery:
    """Validate model-authored *sql* against *policy* and render it for execution.

    Raises `QueryRejectedError` for anything that is not one plain,
    allowlisted, read-only SELECT. On success the returned `GuardedQuery.sql`
    is the canonical rendering, wrapped as `SELECT * FROM (...) LIMIT
    row_limit + 1` so the database itself stops producing rows one past the
    cap and the caller can report truncation.

    Every failure inside the guard is a rejection with a fixed text: an
    exception escaping here would abort the agent's whole turn instead of
    giving the model a result it can act on.
    """
    if row_limit < 1:
        raise ValueError("row_limit must be positive.")
    if not isinstance(sql, str) or not sql.strip():
        raise _reject("invalid_input")
    if len(sql) > policy.max_sql_length:
        raise _reject(
            "too_long",
            f"The query is longer than {policy.max_sql_length} characters; "
            "send a shorter one.",
        )
    try:
        return _guard(sql, policy, row_limit)
    except QueryRejectedError:
        raise
    except RecursionError as error:
        raise _too_complex(policy) from error
    except Exception as error:  # any other failure is a refusal, never a crash
        raise _reject("unparseable") from error


def _guard(sql: str, policy: QueryPolicy, row_limit: int) -> GuardedQuery:
    statement, relations = _validate(sql, policy, max_nodes=policy.max_nodes)
    rendered = _render(statement, policy)

    # Fixed point: the rendering is what executes, so it must itself pass
    # every check and render back to exactly the same text. A renderer that
    # wrote a construct back in a form that reads differently fails here
    # instead of reaching the database with an unchecked meaning.
    # The rendering only adds a schema to each relation (one identifier per
    # table), so its tree is at most twice the size the budget admitted.
    try:
        executed, relations = _validate(
            rendered, policy, max_nodes=2 * policy.max_nodes
        )
        rerendered = _render(executed, policy)
    except QueryRejectedError as error:
        raise _reject("unparseable") from error
    if rerendered != rendered:
        raise _reject("unparseable")

    wrapped = (
        exp.select("*").from_(executed.subquery(_RESULT_ALIAS)).limit(row_limit + 1)
    )
    final = wrapped.sql(dialect=_DIALECT, identify=True, comments=False)
    if f"({rendered})" not in final:
        raise _reject("unparseable")
    return GuardedQuery(sql=final, relations=tuple(sorted(set(relations))))
