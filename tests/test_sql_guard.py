"""Offline tests for the application-layer SQL guard (#80, ADR-007).

The guard is the SECOND layer of the read-only SQL tool: the database role is
the boundary (see `tests/test_sql_query_integration.py`), and this layer must
still hold on its own. Every test here runs without a database: the guard
parses model-authored text with a real PostgreSQL-dialect parser, never with
string matching, and returns the canonical rendering that is the ONLY text the
connector ever sends to the database.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import pytest

from agents_system.services.sql_guard import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_MAX_SQL_LENGTH,
    QueryPolicy,
    QueryRejectedError,
    guard_query,
    parse_relation_name,
)

_POLICY = QueryPolicy(
    allowed_relations=frozenset(
        {("reporting", "sales_v"), ("reporting", "clients_v"), ("archive", "sales_v")}
    ),
)
_SINGLE_SCHEMA_POLICY = QueryPolicy(
    allowed_relations=frozenset({("reporting", "sales_v"), ("reporting", "clients_v")}),
)


def _reject_code(sql: object, policy: QueryPolicy = _SINGLE_SCHEMA_POLICY) -> str:
    with pytest.raises(QueryRejectedError) as excinfo:
        guard_query(sql, policy, row_limit=10)
    return excinfo.value.code


# ---------------------------------------------------------------------------
# Accepted queries and the canonical rendering
# ---------------------------------------------------------------------------


def test_plain_select_is_accepted_and_rendered_qualified_and_quoted() -> None:
    guarded = guard_query(
        "select product, sum(amount) from sales_v group by product",
        _SINGLE_SCHEMA_POLICY,
        row_limit=10,
    )

    assert '"reporting"."sales_v"' in guarded.sql
    assert guarded.relations == ("reporting.sales_v",)


def test_rendering_wraps_the_query_with_a_row_limit_of_n_plus_one() -> None:
    guarded = guard_query(
        "SELECT product FROM reporting.sales_v ORDER BY product",
        _SINGLE_SCHEMA_POLICY,
        row_limit=25,
    )

    assert guarded.sql.endswith("LIMIT 26")
    assert guarded.sql.startswith("SELECT * FROM (")


def test_a_single_trailing_semicolon_is_accepted() -> None:
    guarded = guard_query(
        "SELECT 1 FROM reporting.sales_v;", _SINGLE_SCHEMA_POLICY, row_limit=10
    )
    assert ";" not in guarded.sql


@pytest.mark.parametrize(
    "sql",
    [
        (
            "WITH m AS (SELECT date_trunc('month', sold_at) AS month, sum(amount) AS t"
            " FROM sales_v GROUP BY 1) SELECT * FROM m ORDER BY month"
        ),
        "SELECT a.product FROM sales_v a JOIN clients_v c ON c.id = a.client_id",
        "SELECT product FROM sales_v WHERE client_id IN (SELECT id FROM clients_v)",
        "SELECT product FROM sales_v UNION SELECT name FROM clients_v",
        (
            "SELECT count(*) FILTER (WHERE amount > 0), avg(amount), max(sold_at)"
            " FROM sales_v"
        ),
        (
            "SELECT product, rank() OVER (ORDER BY sum(amount) DESC) FROM sales_v"
            " GROUP BY product"
        ),
        (
            "SELECT to_char(sold_at, 'YYYY-MM'), round(sum(amount)::numeric, 2)"
            " FROM sales_v GROUP BY 1"
        ),
        (
            "SELECT extract(year FROM sold_at), coalesce(nullif(product, ''), 'n/a')"
            " FROM sales_v"
        ),
        (
            "SELECT d::date FROM generate_series(current_date - 7, current_date,"
            " interval '1 day') AS d"
        ),
        (
            "SELECT lower(name), length(name), string_agg(product, ', ')"
            " FROM clients_v JOIN sales_v ON true GROUP BY 1, 2"
        ),
        "SELECT * FROM sales_v s WHERE s.amount > 0 ORDER BY s.sold_at DESC LIMIT 5",
        (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3)"
            " SELECT * FROM r"
        ),
        (
            "SELECT product FROM sales_v WHERE product ILIKE '%x%'"
            " AND sold_at >= now() - interval '30 days'"
        ),
    ],
)
def test_typical_analytical_queries_are_accepted(sql: str) -> None:
    guarded = guard_query(sql, _SINGLE_SCHEMA_POLICY, row_limit=10)
    assert guarded.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE WHEN amount > 100 THEN 'big' ELSE 'small' END FROM sales_v",
        "SELECT CASE product WHEN 'a' THEN 1 WHEN 'b' THEN 2 END FROM sales_v",
        "SELECT sum(CASE WHEN amount > 0 THEN amount ELSE 0 END) FROM sales_v",
        (
            "SELECT CASE WHEN amount > 0 THEN CASE WHEN amount > 100 THEN 'big'"
            " ELSE 'small' END END FROM sales_v"
        ),
    ],
)
def test_case_expressions_are_accepted(sql: str) -> None:
    # sqlglot writes each WHEN branch of a CASE from inside the CASE, never
    # as a call of its own: the branch is syntax, not an unchecked function.
    guarded = guard_query(sql, _SINGLE_SCHEMA_POLICY, row_limit=10)
    assert "CASE" in guarded.sql and "END" in guarded.sql


# ---------------------------------------------------------------------------
# Input shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [None, 42, ["SELECT 1"], {"sql": "SELECT 1"}])
def test_non_string_input_is_rejected(sql: object) -> None:
    assert _reject_code(sql) == "invalid_input"


@pytest.mark.parametrize("sql", ["", "   ", ";", "-- only a comment"])
def test_empty_input_is_rejected(sql: str) -> None:
    assert _reject_code(sql) in {"invalid_input", "not_select"}


def test_overlong_input_is_rejected_before_parsing() -> None:
    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, max_sql_length=50
    )
    assert _reject_code("SELECT " + "1 + " * 40 + "1", policy) == "too_long"


def test_unparseable_input_is_rejected() -> None:
    assert _reject_code("SELECT FROM WHERE (((") == "unparseable"


# ---------------------------------------------------------------------------
# The guard's own cost is bounded
# ---------------------------------------------------------------------------

_OR_TERM = "amount = 1"


def _or_chain(terms: int) -> str:
    return "SELECT 1 FROM sales_v WHERE " + " OR ".join([_OR_TERM] * terms)


def _guard_seconds(sql: str, policy: QueryPolicy = _SINGLE_SCHEMA_POLICY) -> float:
    started = time.perf_counter()
    with contextlib.suppress(QueryRejectedError):
        guard_query(sql, policy, row_limit=10)
    return time.perf_counter() - started


def test_guard_time_stays_linear_on_a_long_boolean_chain() -> None:
    # AND and OR are function nodes in sqlglot's tree. Rendering each one on
    # its own to read its name re-rendered the whole chain below it, so the
    # guard was quadratic: about 3 s for 400 terms, 30 s at the length cap.
    assert _guard_seconds(_or_chain(400)) < 1.0


def test_a_max_length_boolean_chain_is_refused_quickly() -> None:
    fits = (DEFAULT_MAX_SQL_LENGTH - len(_or_chain(1))) // len(f" OR {_OR_TERM}")
    sql = _or_chain(fits + 1)
    assert len(sql) <= DEFAULT_MAX_SQL_LENGTH

    assert _reject_code(sql) == "too_complex"
    assert _guard_seconds(sql) < 1.0


def test_a_query_over_the_node_budget_is_refused() -> None:
    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, max_nodes=50
    )

    assert _reject_code(_or_chain(20), policy) == "too_complex"
    assert guard_query(_or_chain(5), policy, row_limit=10).sql


def test_the_node_budget_applies_to_the_query_not_to_its_qualified_rendering() -> None:
    # The rendering adds a schema to every relation, so it can outgrow the
    # budget the query itself met; that must not turn into a refusal.
    ctes = ", ".join(f"c{i} AS (SELECT * FROM sales_v)" for i in range(10))
    sql = f"WITH {ctes} SELECT 1"
    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, max_nodes=85
    )

    assert guard_query(sql, policy, row_limit=10).sql


#: sqlglot's parser is not linear: a bracket opened right after a data-type
#: keyword is parsed as a type, abandoned, and parsed again as an expression,
#: so every level doubles the work. The length and node caps only applied
#: after parsing: at depth 19 (141 characters) this took 33 s, and was then
#: accepted. A worker thread does not bound it either - it keeps parsing
#: after the caller gives up.
_NESTED_ARRAY_19 = "SELECT " + "ARRAY[" * 19 + "1" + "]" * 19

#: Tests must finish well inside this, including on a slow CI runner.
_GUARD_BUDGET_S = 0.2


def _best_guard_seconds(sql: str, policy: QueryPolicy = _SINGLE_SCHEMA_POLICY) -> float:
    """Best of three runs: a real blow-up is slow every time, noise is not."""
    return min(_guard_seconds(sql, policy) for _ in range(3))


def test_nested_array_is_refused_before_the_parser_runs() -> None:
    assert _reject_code(_NESTED_ARRAY_19) == "too_complex"
    assert _best_guard_seconds(_NESTED_ARRAY_19) < _GUARD_BUDGET_S


def _nest(opener: str, closer: str, depth: int, core: str = "1") -> str:
    return opener * depth + core + closer * depth


def test_brackets_opened_by_a_type_keyword_are_capped() -> None:
    from agents_system.services import sql_guard

    cap = sql_guard.MAX_TYPE_NESTING

    assert guard_query(
        "SELECT " + _nest("ARRAY[", "]", cap), _SINGLE_SCHEMA_POLICY, row_limit=5
    ).sql
    for opener, closer in (
        ("ARRAY[", "]"),
        ("int[", "]"),
        ("numeric(", ")"),
        ("struct(", ")"),
    ):
        assert _reject_code("SELECT " + _nest(opener, closer, cap + 1)) == "too_complex"


def test_nesting_deeper_than_the_cap_is_refused() -> None:
    from agents_system.services import sql_guard

    depth = sql_guard.DEFAULT_MAX_DEPTH

    assert guard_query(
        "SELECT " + _nest("(", ")", depth), _SINGLE_SCHEMA_POLICY, row_limit=5
    ).sql
    for sql in (
        "SELECT " + _nest("(", ")", depth + 1),
        "SELECT " + _nest("(SELECT ", ")", depth + 1),
        "SELECT " + _nest("CASE WHEN true THEN ", " END", depth + 1),
        # A subscript chain nests too: the parser recurses once per link.
        "SELECT ARRAY[1]" + "[1]" * (depth + 1),
    ):
        assert _reject_code(sql) == "too_complex"


def test_size_and_nesting_are_refused_before_the_parser_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents_system.services import sql_guard

    def no_parser(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the parser must not run")

    monkeypatch.setattr(sql_guard, "_parse_single_statement", no_parser)

    too_many_tokens = "SELECT " + ", ".join(["1"] * sql_guard.DEFAULT_MAX_TOKENS)
    assert _reject_code(too_many_tokens) == "too_complex"
    assert _reject_code(_NESTED_ARRAY_19) == "too_complex"
    assert (
        _reject_code("SELECT " + _nest("(", ")", sql_guard.DEFAULT_MAX_DEPTH + 1))
        == "too_complex"
    )


def test_brackets_inside_strings_identifiers_and_comments_do_not_count() -> None:
    sql = (
        "SELECT '"
        + "(" * 50
        + "' AS \""
        + "[" * 50
        + '" FROM sales_v /* '
        + "ARRAY[" * 50
        + " */"
    )
    assert guard_query(sql, _SINGLE_SCHEMA_POLICY, row_limit=5).sql


def test_a_tree_nested_without_brackets_is_capped() -> None:
    # `- - - 1` and `1::int::int` nest in the tree, not in the text; the
    # tree's depth is checked before anything walks it recursively.
    assert guard_query(
        "SELECT " + "- " * 10 + "1", _SINGLE_SCHEMA_POLICY, row_limit=5
    ).sql
    assert _reject_code("SELECT " + "- " * 100 + "1") == "too_complex"
    assert _reject_code("SELECT 1" + "::int" * 100) == "too_complex"


def test_the_parser_itself_works_within_the_node_budget() -> None:
    # Four typed levels build more nodes while parsing (the abandoned type
    # attempts) than the tree keeps: the budget counts the parser's work.
    sql = "SELECT " + _nest("ARRAY[", "]", 4)
    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, max_nodes=12
    )

    assert _reject_code(sql, policy) == "too_complex"
    assert guard_query("SELECT ARRAY[1]", policy, row_limit=5).sql


def _fill(prefix: str, unit: str, suffix: str = "", sep: str = "") -> str:
    """*prefix*, then as many *unit*s as fit under the token cap, then *suffix*."""
    from agents_system.services import sql_guard

    def tokens(text: str) -> int:
        return len(sql_guard._tokenize(text))

    count = (sql_guard.DEFAULT_MAX_TOKENS - tokens(prefix + suffix)) // tokens(
        unit + sep
    )
    while count > 1 and tokens(prefix + sep.join([unit] * count) + suffix) > (
        sql_guard.DEFAULT_MAX_TOKENS
    ):
        count -= 1
    return prefix + sep.join([unit] * count) + suffix


def _worst_cases() -> dict[str, str]:
    from agents_system.services import sql_guard

    depth = sql_guard.DEFAULT_MAX_DEPTH
    typed = sql_guard.MAX_TYPE_NESTING
    pad = (depth - typed) // typed

    def typed_nest(opener: str, closer: str) -> str:
        return (
            "SELECT "
            + (opener + "(" * pad) * typed
            + "1"
            + (")" * pad + closer) * typed
        )

    return {
        # Exponential in the parser: brackets opened by a type keyword, at
        # the type cap with the rest of the depth cap filled by parentheses,
        # and far past it.
        "array-at-caps": typed_nest("ARRAY[", "]"),
        "int-array-at-caps": typed_nest("int[", "]"),
        "numeric-at-caps": typed_nest("numeric(", ")"),
        "struct-at-caps": typed_nest("struct(", ")"),
        "array-depth-19": _NESTED_ARRAY_19,
        "array-depth-40": "SELECT " + _nest("ARRAY[", "]", 40),
        "struct-depth-40": "SELECT " + _nest("struct(", ")", 40),
        # Quadratic in the parser: a subscript chain.
        "subscript-chain": _fill("SELECT ARRAY[1]", "[1]"),
        # Nesting at the depth cap.
        "parens-at-depth": "SELECT " + _nest("(", ")", depth),
        "scalar-subqueries-at-depth": "SELECT " + _nest("(SELECT ", ")", depth),
        "from-subqueries-at-depth": (
            "SELECT * FROM " + "(SELECT * FROM " * depth + "sales_v" + ") AS t" * depth
        ),
        "exists-at-depth": "SELECT " + _nest("EXISTS (SELECT ", ")", depth),
        "case-at-depth": "SELECT " + _nest("CASE WHEN true THEN ", " END", depth),
        # Breadth at the token cap.
        "or-chain": _fill("SELECT 1 FROM sales_v WHERE ", "amount = 1", sep=" OR "),
        "and-chain": _fill("SELECT 1 FROM sales_v WHERE ", "amount > 1", sep=" AND "),
        "in-list": _fill("SELECT 1 FROM sales_v WHERE amount IN (", "1", ")", sep=", "),
        "case-branches": _fill(
            "SELECT CASE ", "WHEN amount = 1 THEN 1 ", "END FROM sales_v"
        ),
        "scalar-subqueries": _fill(
            "SELECT ", "(SELECT max(amount) FROM sales_v)", sep=", "
        ),
        "ctes": _fill("WITH ", "c AS (SELECT * FROM sales_v)", " SELECT 1", sep=", "),
        "joins": _fill("SELECT 1 FROM sales_v AS t ", "JOIN sales_v ON true "),
        "windows": _fill(
            "SELECT ",
            "sum(amount) OVER (PARTITION BY product ORDER BY amount)",
            " FROM sales_v",
            sep=", ",
        ),
        "unions": _fill("", "SELECT amount FROM sales_v", sep=" UNION ALL "),
        # Nesting without brackets, and the tokenizer at the length cap.
        "unary-chain": _fill("SELECT ", "- ", "1"),
        "cast-chain": _fill("SELECT 1", "::int"),
        "long-string": "SELECT '" + "x" * (DEFAULT_MAX_SQL_LENGTH - 12) + "'",
        "open-brackets": "SELECT " + "[" * (DEFAULT_MAX_SQL_LENGTH - 7),
        "unterminated-comments": "SELECT 1 /*"
        + "/*" * ((DEFAULT_MAX_SQL_LENGTH - 12) // 2),
    }


@pytest.mark.parametrize("shape", list(_worst_cases()))
def test_worst_cases_at_the_caps_finish_within_a_fixed_budget(shape: str) -> None:
    sql = _worst_cases()[shape]
    assert len(sql) <= DEFAULT_MAX_SQL_LENGTH

    assert _best_guard_seconds(sql) < _GUARD_BUDGET_S


@pytest.mark.parametrize("field", ["max_tokens", "max_depth"])
def test_the_new_caps_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError):
        QueryPolicy(
            allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, **{field: 0}
        )


@pytest.mark.parametrize(
    "sql",
    [
        # Short texts whose trees are deep enough to exhaust Python's
        # recursion limit in the renderer: unary minus, `::` cast chains and
        # nested FROM subqueries.
        "SELECT " + "- " * 200 + "1",
        "SELECT 1" + "::int" * 170,
        "SELECT * FROM " + "(SELECT * FROM " * 60 + "sales_v" + ") AS t" * 60,
    ],
    ids=["unary-minus-200", "cast-chain-170", "from-subquery-60"],
)
def test_a_deeply_nested_query_is_a_rejection_not_an_exception(sql: str) -> None:
    # An exception escaping the guard aborts the whole agent turn; a
    # rejection is a tool result the model can act on.
    assert _reject_code(sql) in {"too_complex", "unparseable"}


@pytest.mark.parametrize(
    "error",
    [RecursionError("deep"), RuntimeError("secret detail"), KeyError("x")],
    ids=lambda error: type(error).__name__,
)
def test_any_failure_inside_the_guard_is_a_fixed_rejection(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from agents_system.services import sql_guard

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(sql_guard, "normalize_identifiers", failing)

    with pytest.raises(QueryRejectedError) as excinfo:
        guard_query("SELECT product FROM sales_v", _SINGLE_SCHEMA_POLICY, row_limit=5)

    assert excinfo.value.code in {"too_complex", "unparseable"}
    assert "secret detail" not in excinfo.value.message


def test_the_node_budget_must_be_positive() -> None:
    with pytest.raises(ValueError):
        QueryPolicy(
            allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations, max_nodes=0
        )


# ---------------------------------------------------------------------------
# Single-statement enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM sales_v; SELECT 2 FROM sales_v",
        "SELECT 1 FROM sales_v; DROP TABLE reporting.sales",
        # A line comment ending the first statement cannot hide the second.
        "SELECT 1 FROM sales_v -- harmless\n; DELETE FROM reporting.sales",
    ],
)
def test_more_than_one_statement_is_rejected(sql: str) -> None:
    assert _reject_code(sql) == "multiple_statements"


def test_a_block_comment_hiding_a_statement_never_reaches_the_database() -> None:
    guarded = guard_query(
        "SELECT 1 FROM sales_v /* ; DROP TABLE reporting.sales */",
        _SINGLE_SCHEMA_POLICY,
        row_limit=10,
    )

    assert "DROP" not in guarded.sql.upper()
    assert "/*" not in guarded.sql


def test_a_nested_block_comment_is_read_the_way_postgres_reads_it() -> None:
    # PostgreSQL nests block comments: everything up to the second `*/` is
    # a comment. A parser that stopped at the first `*/` would see a
    # subquery against a relation it never checked.
    guarded = guard_query(
        "SELECT 1 FROM sales_v /* a /* b */ , (SELECT x FROM secret) */",
        _SINGLE_SCHEMA_POLICY,
        row_limit=10,
    )
    assert "secret" not in guarded.sql


# ---------------------------------------------------------------------------
# The rendering is what executes, so it must mean what was validated
# ---------------------------------------------------------------------------

# In PostgreSQL, E'\\' is ONE backslash, and the second literal hides a
# subquery as plain text. A renderer that writes that literal back as e'\'
# turns `\'` into an escaped quote: the string swallows the text up to the
# next quote and the "hidden" subquery against `secret` becomes live SQL
# the guard never checked.
_ESCAPE_STRING_BREAKOUT = (
    "SELECT E'\\\\' AS a, ' , note FROM secret) AS t --' FROM sales_v"
)


def test_escape_string_rendering_cannot_smuggle_a_relation() -> None:
    assert _reject_code(_ESCAPE_STRING_BREAKOUT) == "unsupported_literal"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT E'x' FROM sales_v",
        "SELECT U&'d\\0061ta' FROM sales_v",
        "SELECT B'101' FROM sales_v",
        "SELECT X'1F' FROM sales_v",
        "SELECT N'abc' FROM sales_v",
        "SELECT $$it's$$ FROM sales_v",
        "SELECT $q$a$q$ FROM sales_v",
    ],
)
def test_only_standard_string_literals_are_accepted(sql: str) -> None:
    assert _reject_code(sql) == "unsupported_literal"


def test_quotes_inside_identifiers_and_strings_stay_escaped() -> None:
    # An identifier or string holding the text of a subquery must still be
    # ONE identifier or string after rendering, never live SQL.
    guarded = guard_query(
        'SELECT 1 AS "a"" , (SELECT x FROM secret) AS ""b", '
        "'it''s , (SELECT x FROM secret)' FROM sales_v",
        _SINGLE_SCHEMA_POLICY,
        row_limit=10,
    )
    assert '"a"" , (SELECT x FROM secret) AS ""b"' in guarded.sql
    assert "'it''s , (SELECT x FROM secret)'" in guarded.sql


def test_a_rendering_that_does_not_revalidate_unchanged_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents_system.services import sql_guard

    real_render = sql_guard._render

    def drifting_render(statement: Any, policy: QueryPolicy) -> str:
        return real_render(statement, policy) + " UNION SELECT note FROM secret"

    monkeypatch.setattr(sql_guard, "_render", drifting_render)

    assert _reject_code("SELECT product FROM sales_v") == "unparseable"


# ---------------------------------------------------------------------------
# DDL / DML / everything that is not a plain SELECT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO reporting.sales_v VALUES (1)",
        "UPDATE reporting.sales_v SET amount = 0",
        "DELETE FROM reporting.sales_v",
        (
            "MERGE INTO reporting.sales_v t USING reporting.clients_v s ON true"
            " WHEN MATCHED THEN DELETE"
        ),
        "DROP TABLE reporting.sales",
        "DROP VIEW reporting.sales_v",
        "CREATE TABLE reporting.x (a int)",
        "CREATE TEMP TABLE x (a int)",
        "ALTER TABLE reporting.sales ADD COLUMN x int",
        "TRUNCATE reporting.sales",
        "GRANT SELECT ON reporting.sales TO PUBLIC",
        "COPY reporting.sales TO '/tmp/out'",
        "COPY (SELECT 1) TO PROGRAM 'id'",
        "SET statement_timeout = 0",
        "SET LOCAL default_transaction_read_only = off",
        "RESET ALL",
        "BEGIN",
        "COMMIT",
        "EXPLAIN ANALYZE SELECT 1",
        "DO $$ BEGIN PERFORM 1; END $$",
        "CALL reporting.proc()",
        "VACUUM",
        "LOCK TABLE reporting.sales",
        "PREPARE p AS SELECT 1",
        "LISTEN channel",
        "TABLE reporting.sales_v",
        "VALUES (1)",
    ],
)
def test_statements_other_than_select_are_rejected(sql: str) -> None:
    assert _reject_code(sql) in {"not_select", "unparseable"}


@pytest.mark.parametrize(
    "sql",
    [
        # SELECT ... INTO creates a table.
        "SELECT * INTO reporting.copy FROM sales_v",
        # Locking clauses take row locks and need UPDATE rights.
        "SELECT * FROM sales_v FOR UPDATE",
        "SELECT * FROM sales_v FOR SHARE",
        "SELECT * FROM sales_v FOR NO KEY UPDATE",
    ],
)
def test_select_forms_that_write_or_lock_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "forbidden_clause"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (DELETE FROM reporting.sales RETURNING *) SELECT * FROM d",
        "WITH u AS (UPDATE reporting.sales SET amount = 0 RETURNING 1) SELECT * FROM u",
        (
            "WITH i AS (INSERT INTO reporting.sales VALUES (1) RETURNING 1)"
            " SELECT * FROM i"
        ),
    ],
)
def test_data_modifying_ctes_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "forbidden_clause"


# ---------------------------------------------------------------------------
# Function abuse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(60)",
        "SELECT pg_sleep_for('1 minute')",
        "SELECT 1 FROM sales_v WHERE pg_sleep(10) IS NOT NULL",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT current_setting('data_directory')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_terminate_backend(1)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT query_to_xml('select * from secret', true, true, '')",
        "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
        "SELECT * FROM pg_ls_dir('.')",
        "SELECT nextval('reporting.seq')",
        "SELECT txid_current()",
    ],
)
def test_functions_outside_the_allowlist_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "function_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        # Schema-qualified calls escape the pinned search path and can reach
        # a deployment's own functions, even when the bare name is allowed.
        "SELECT public.count(1)",
        "SELECT pg_catalog.pg_sleep(1)",
        # A quoted name is not what the allowlist matched against.
        'SELECT "pg_sleep"(1)',
        # Operators are functions too; a qualified one can be user-defined.
        "SELECT 1 OPERATOR(public.+) 2",
    ],
)
def test_qualified_or_quoted_function_and_operator_calls_are_rejected(
    sql: str,
) -> None:
    assert _reject_code(sql) == "function_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        # A table function in FROM is a Table node, not a Dot: its schema sits
        # on the Table. An allowlisted bare name must not make the qualified
        # call acceptable - it could be a deployment-owned SECURITY DEFINER
        # function that reads what the role cannot.
        "SELECT * FROM public.generate_series(1, 3)",
        "SELECT * FROM evil.unnest(ARRAY[1])",
        "SELECT * FROM reporting.generate_series(1, 3) AS g",
        "SELECT * FROM public.lower('x')",
        "SELECT * FROM public.count()",
        'SELECT * FROM "public"."generate_series"(1, 3)',
        "SELECT * FROM mydb.public.generate_series(1, 3)",
        "SELECT * FROM pg_catalog.generate_series(1, 2)",
        "SELECT * FROM public.lower('x') WITH ORDINALITY",
        "SELECT * FROM ROWS FROM (public.generate_series(1, 2))",
        "SELECT * FROM sales_v JOIN public.generate_series(1, 2) AS g ON true",
        "SELECT * FROM sales_v, LATERAL public.generate_series(1, 2)",
        "SELECT * FROM sales_v CROSS JOIN LATERAL public.unnest(ARRAY[1]) AS u",
    ],
)
def test_qualified_table_functions_are_rejected_in_every_from_position(
    sql: str,
) -> None:
    assert _reject_code(sql) == "function_not_allowed"


def test_an_unqualified_table_function_stays_accepted() -> None:
    guarded = guard_query(
        "SELECT g FROM generate_series(1, 3) AS g", _SINGLE_SCHEMA_POLICY, row_limit=5
    )
    assert "GENERATE_SERIES(1, 3)" in guarded.sql.upper()


@pytest.mark.parametrize(
    "template",
    [
        "SELECT 1 FROM sales_v WHERE a = 1 AND b = 2 AND {f} IS NULL",
        "SELECT 1 FROM sales_v WHERE a = 1 OR {f} IS NULL OR b = 2",
        "SELECT 1 + 2 + {f} + 3",
        "SELECT CASE WHEN {f} IS NULL THEN 1 END",
        "SELECT CASE WHEN true THEN {f} END",
        "SELECT CASE WHEN true THEN 1 ELSE {f} END",
        "SELECT CASE {f} WHEN 1 THEN 1 END",
        "SELECT count(*) FILTER (WHERE {f} IS NULL) FROM sales_v",
        "SELECT sum(1) OVER (PARTITION BY {f}) FROM sales_v",
        "WITH c AS (SELECT {f}) SELECT * FROM c",
        "SELECT * FROM generate_series(1, {f}::int)",
        "SELECT * FROM sales_v, LATERAL (SELECT {f}) AS x",
        "SELECT ARRAY[{f}]",
        "SELECT CAST({f} AS text)",
        "SELECT 1 UNION SELECT {f}",
        "SELECT EXISTS (SELECT {f})",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {f}) FROM sales_v",
        "SELECT 1 FROM sales_v LIMIT {f}",
        "SELECT extract(year FROM {f})",
    ],
)
def test_a_disallowed_function_is_found_in_every_position(template: str) -> None:
    # Function names are checked while the statement is rendered; every
    # position a call can take must pass through that check.
    assert _reject_code(template.format(f="pg_sleep(1)")) == "function_not_allowed"


def test_a_function_the_renderer_did_not_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlglot import exp
    from sqlglot.generators.postgres import PostgresGenerator

    from agents_system.services import sql_guard

    def skipping_sql(
        self: Any, expression: Any, key: str | None = None, comment: bool = True
    ) -> str:
        if isinstance(expression, exp.Anonymous):
            return PostgresGenerator.sql(self, expression, key, comment)
        return real_sql(self, expression, key, comment)

    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations,
        allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"deployment_fn"},
    )
    sql = "SELECT deployment_fn(product) FROM sales_v"
    assert guard_query(sql, policy, row_limit=5).sql

    real_sql = sql_guard._CheckedRenderer.sql
    monkeypatch.setattr(sql_guard._CheckedRenderer, "sql", skipping_sql)

    assert _reject_code(sql, policy) == "unparseable"


@pytest.mark.parametrize(
    "sql",
    ["SELECT $1", "SELECT * FROM sales_v WHERE amount > :minimum", "SELECT ?"],
)
def test_bind_parameters_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "parameter_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount::public.money_type FROM sales_v",
        "SELECT 'reporting.secret'::regclass",
    ],
)
def test_casts_to_non_builtin_or_catalog_lookup_types_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "type_not_allowed"


def test_default_function_allowlist_holds_no_side_effecting_function() -> None:
    dangerous = {
        "pg_sleep",
        "set_config",
        "current_setting",
        "pg_advisory_lock",
        "query_to_xml",
        "dblink",
        "lo_import",
        "pg_read_file",
        "nextval",
        "setval",
    }
    assert not dangerous & DEFAULT_ALLOWED_FUNCTIONS


def test_a_deployment_can_extend_the_function_allowlist() -> None:
    policy = QueryPolicy(
        allowed_relations=_SINGLE_SCHEMA_POLICY.allowed_relations,
        allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"md5"},
    )
    assert guard_query("SELECT md5(product) FROM sales_v", policy, row_limit=5).sql


# ---------------------------------------------------------------------------
# Relation allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secret",
        "SELECT * FROM reporting.sales",
        "SELECT * FROM public.sales_v",
        "SELECT * FROM other.sales_v",
        "SELECT * FROM pg_catalog.pg_user",
        "SELECT * FROM pg_user",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM sales_v JOIN secret ON true",
        "SELECT (SELECT max(x) FROM secret) FROM sales_v",
        "SELECT * FROM sales_v WHERE EXISTS (SELECT 1 FROM reporting.secret)",
        "SELECT ARRAY(SELECT x FROM secret)",
        "SELECT * FROM sales_v UNION ALL SELECT * FROM secret",
        "WITH x AS (SELECT * FROM secret) SELECT * FROM x",
        # Cross-database references are not a PostgreSQL feature and never
        # match an allowlist entry.
        "SELECT * FROM otherdb.reporting.sales_v",
    ],
)
def test_relations_outside_the_allowlist_are_rejected(sql: str) -> None:
    assert _reject_code(sql) == "relation_not_allowed"


def test_schema_qualified_allowlisted_view_is_accepted() -> None:
    guarded = guard_query(
        "SELECT * FROM reporting.clients_v", _SINGLE_SCHEMA_POLICY, row_limit=5
    )
    assert guarded.relations == ("reporting.clients_v",)


def test_unquoted_identifiers_fold_to_lower_case_like_postgres() -> None:
    guarded = guard_query(
        "SELECT * FROM Reporting.SALES_V", _SINGLE_SCHEMA_POLICY, row_limit=5
    )
    assert guarded.relations == ("reporting.sales_v",)


def test_quoted_lower_case_identifiers_match_the_allowlist() -> None:
    guarded = guard_query(
        'SELECT * FROM "reporting"."sales_v"', _SINGLE_SCHEMA_POLICY, row_limit=5
    )
    assert guarded.relations == ("reporting.sales_v",)


@pytest.mark.parametrize(
    "sql", ['SELECT * FROM "SALES_V"', 'SELECT * FROM "Reporting".sales_v']
)
def test_quoted_identifiers_are_case_sensitive_like_postgres(sql: str) -> None:
    assert _reject_code(sql) == "relation_not_allowed"


def test_an_unqualified_name_in_two_allowlisted_schemas_is_ambiguous() -> None:
    assert _reject_code("SELECT * FROM sales_v", _POLICY) == "ambiguous_relation"


def test_a_cte_named_like_a_relation_is_a_cte_only_inside_its_own_scope() -> None:
    # The outer `secret` is the real relation: the CTE of the same name is
    # visible only inside the LATERAL subquery. Treating every `secret` as
    # the CTE would skip the allowlist check PostgreSQL itself never skips.
    sql = (
        "SELECT * FROM secret, LATERAL (WITH secret AS (SELECT 1 AS a)"
        " SELECT * FROM secret) AS s"
    )
    assert _reject_code(sql) == "relation_not_allowed"


def test_a_cte_is_not_mistaken_for_a_relation() -> None:
    guarded = guard_query(
        "WITH secret AS (SELECT * FROM sales_v) SELECT * FROM secret",
        _SINGLE_SCHEMA_POLICY,
        row_limit=5,
    )
    assert guarded.relations == ("reporting.sales_v",)


def test_a_qualified_name_is_never_resolved_to_a_cte() -> None:
    sql = "WITH secret AS (SELECT 1) SELECT * FROM reporting.secret"
    assert _reject_code(sql) == "relation_not_allowed"


# ---------------------------------------------------------------------------
# Rejection texts are fixed and safe
# ---------------------------------------------------------------------------


def test_relation_rejection_names_the_queryable_views() -> None:
    with pytest.raises(QueryRejectedError) as excinfo:
        guard_query("SELECT * FROM secret", _SINGLE_SCHEMA_POLICY, row_limit=5)

    message = excinfo.value.message
    assert "reporting.sales_v" in message
    assert "reporting.clients_v" in message


def test_rejection_never_echoes_more_than_an_identifier_of_the_input() -> None:
    long_name = "x" * 500
    with pytest.raises(QueryRejectedError) as excinfo:
        guard_query(f"SELECT * FROM {long_name}", _SINGLE_SCHEMA_POLICY, row_limit=5)

    # PostgreSQL truncates identifiers at 63 bytes; so does the echo.
    assert "x" * 64 not in excinfo.value.message


# ---------------------------------------------------------------------------
# Allowlist configuration
# ---------------------------------------------------------------------------


def test_parse_relation_name_requires_schema_and_view() -> None:
    assert parse_relation_name("reporting.sales_v") == ("reporting", "sales_v")


@pytest.mark.parametrize(
    "name",
    ["sales_v", "a.b.c", "Reporting.sales_v", 'reporting."x"', "reporting.", ""],
)
def test_parse_relation_name_rejects_anything_but_simple_lower_case_names(
    name: str,
) -> None:
    with pytest.raises(ValueError):
        parse_relation_name(name)


def test_policy_requires_at_least_one_relation() -> None:
    with pytest.raises(ValueError):
        QueryPolicy(allowed_relations=frozenset())
