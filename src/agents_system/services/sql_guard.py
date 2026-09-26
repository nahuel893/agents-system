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
- The guard's own cost is bounded BEFORE the parser runs, because sqlglot's
  parser is not linear: it backtracks when a data-type keyword (`ARRAY`,
  `numeric`, `int`, `struct`, ...) opens a bracket, and every such level
  doubles the work (nested `ARRAY[...]` took 33 s at 141 characters). The
  text is capped at `max_sql_length` characters, then tokenized (a linear
  scan) and refused unless the parser would read at most `max_tokens`
  tokens (a token inside `k` brackets opened by a type keyword counts
  `2**k`), it opens at most `MAX_SQUARE_BRACKETS` square brackets (each
  subscript costs the parser about twenty tokens' work), and it has at most
  `max_depth` levels of bracket/CASE nesting and at most `MAX_TYPE_NESTING`
  levels opened by a type keyword; every level must be closed by its own
  token (an END that closes no CASE is refused, see `_check_tokens`). The
  parser then runs with a budget on the
  nodes it builds, abandoned attempts included, and the parsed tree must
  stay within `max_nodes` nodes and `4 * max_depth` levels (long AND/OR and
  same-operator chains, which sqlglot handles iteratively, count as one), so
  nothing downstream recurses deeper than Python allows. Every later check
  is a linear pass (function names are checked during the one render of the
  statement, never by rendering each node on its own). Any failure inside
  the guard is a rejection with a fixed text, never an exception.
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

from sqlglot import Token, TokenType, exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError
from sqlglot.generators.postgres import PostgresGenerator
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

_DIALECT = "postgres"
_POSTGRES = Dialect.get_or_raise(_DIALECT)
_PG_IDENTIFIER_MAX = 63
_SIMPLE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_CALL_NAME = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_RESULT_ALIAS = "sql_query_result"

DEFAULT_MAX_SQL_LENGTH = 10_000
"""Longest query text the guard tokenizes. A real analytical question fits in
a fraction of this; anything longer is refused before any other work."""

DEFAULT_MAX_TOKENS = 600
"""Most token reads (keywords, names, literals, operators) the guard lets the
parser make. A token counts once, and once more for every time the parser
reads it again: inside `k` brackets opened by a type keyword it counts
`2**k` (see `MAX_TYPE_NESTING`). A real analytical query has a few hundred
tokens, each read once; the cap is checked on the token stream, before the
parser runs."""

DEFAULT_MAX_DEPTH = 20
"""Deepest nesting of brackets, parentheses and CASE ... END the guard
parses, checked on the token stream before the parser runs. A real query
nests a handful of levels; the parser recurses per level, and so does
everything downstream of it."""

MAX_TYPE_NESTING = 4
"""Most brackets that may be open at once where each was opened right after
a data-type keyword (`ARRAY[`, `numeric(`, `int[`, `struct(`). sqlglot
tries each such bracket as a type first and then parses it again as an
expression, so the work doubles per level; four levels cover arrays of
arrays and typed casts. What those levels enclose is also paid for in
`max_tokens`: each token inside `k` of them counts `2**k`."""

MAX_SQUARE_BRACKETS = 16
"""Most square brackets (array constructors, subscripts, slices) a query may
open, counted before the parser runs. sqlglot re-analyses every subscript's
index while it parses it (type annotation and simplification), about twenty
plain tokens' worth of work: at the token cap, `x[1], x[1], ...` took nearly
three times as long as any other shape. A real query uses a handful."""

DEFAULT_MAX_NODES = 2_500
"""Most syntax-tree nodes the guard validates, and most nodes the parser may
build while reading the text (abandoned attempts included). A real
analytical query has a few hundred at most."""

_TREE_LEVELS_PER_DEPTH = 4
"""Tree levels one nesting level may add (a subquery in FROM is a Subquery,
a Select, a From and a Table node): the parsed tree may be at most
`_TREE_LEVELS_PER_DEPTH * max_depth` levels deep. That also bounds what the
text cannot show - `- - - 1` or `1::int::int` nest without brackets - and
keeps every recursive pass after parsing far from Python's recursion
limit."""

_OPENERS = frozenset({TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE})
_CLOSES = {
    TokenType.L_PAREN: TokenType.R_PAREN,
    TokenType.L_BRACKET: TokenType.R_BRACKET,
    TokenType.L_BRACE: TokenType.R_BRACE,
    TokenType.CASE: TokenType.END,
}
"""The one token that closes each level the pre-scan opens."""
_LEVEL_CLOSERS = frozenset(_CLOSES.values())
_TYPE_TOKENS = frozenset(_POSTGRES.parser_class.TYPE_TOKENS)

DEFAULT_ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Aggregates.
        "count",
        "sum",
        "avg",
        "min",
        "max",
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
        "replace",
        "reverse",
        "concat",
        "concat_ws",
        "split_part",
        "position",
        "strpos",
        "regexp_replace",
        "overlay",
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
string (`query_to_xml` and friends would bypass the relation allowlist).

Left out on purpose, though harmless in every other way: functions that
build a large value from short input. `rpad`/`lpad` take a length and
`format` a width (`rpad('x', 250000000)` asks for 250 MB), and `string_agg`,
`array_agg`, `json_agg` and `jsonb_agg` keep every row's value in memory.
PostgreSQL has no per-query memory limit: a few such columns in one short
query got a database in a 1 GB container OOM-killed, and the whole cluster
restarted. Leaving them out does not bound memory - plain `||` through
chained subqueries doubles a value per level - it only stops one short call
from asking for gigabytes; the bound is the database host's (ADR-007).

A deployment may pass a larger set; it then owns the review of what it adds.
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
    """What a guarded query may touch (relations, functions) and how large it
    may be (text length, tokens, nesting depth, tree nodes)."""

    allowed_relations: frozenset[tuple[str, str]]
    allowed_functions: frozenset[str] = field(default=DEFAULT_ALLOWED_FUNCTIONS)
    max_sql_length: int = DEFAULT_MAX_SQL_LENGTH
    max_nodes: int = DEFAULT_MAX_NODES
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_depth: int = DEFAULT_MAX_DEPTH

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
        for name in ("max_sql_length", "max_nodes", "max_tokens", "max_depth"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive.")

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


@dataclass(frozen=True)
class _Limits:
    """The size and depth caps one validation pass applies."""

    tokens: int
    depth: int
    nodes: int
    tree_depth: int
    square_brackets: int

    @classmethod
    def of(cls, policy: QueryPolicy) -> _Limits:
        return cls(
            tokens=policy.max_tokens,
            depth=policy.max_depth,
            nodes=policy.max_nodes,
            tree_depth=_TREE_LEVELS_PER_DEPTH * policy.max_depth,
            square_brackets=MAX_SQUARE_BRACKETS,
        )

    def for_rendering(self) -> _Limits:
        """Caps for the guard's own rendering of a tree these caps admitted.

        The rendering adds a schema to every relation and writes `x::t` as
        `CAST(x AS t)`: at most twice the tokens, nodes and square brackets,
        and no more bracket levels than the tree it was written from has
        levels.
        """
        return _Limits(
            tokens=2 * self.tokens,
            depth=self.tree_depth,
            nodes=2 * self.nodes,
            tree_depth=self.tree_depth,
            square_brackets=2 * self.square_brackets,
        )


def _too_complex() -> QueryRejectedError:
    return _reject(
        "too_complex",
        "The query is too large or too deeply nested for this tool. Simplify "
        "it: fewer conditions or expressions, fewer nested subqueries or "
        "brackets, or aggregate in fewer steps.",
    )


def _tokenize(sql: str) -> list[Token]:
    try:
        return _POSTGRES.tokenize(sql)
    except Exception as error:  # any tokenizer failure is a refusal
        raise _reject("unparseable") from error


def _check_tokens(tokens: list[Token], limits: _Limits) -> None:
    """Refuse, in one pass over the tokens, what would make the parser slow.

    Runs before the parser: the token reads bound its work (a token inside
    `k` brackets opened by a type keyword is read `2**k` times, see
    `MAX_TYPE_NESTING`), the square brackets bound its costliest construct
    (see `MAX_SQUARE_BRACKETS`), the nesting depth bounds its recursion, and
    the type nesting bounds its backtracking. A bracket or CASE inside a
    string, a quoted identifier or a comment is part of that one token and
    does not count.

    Every level must be closed by its own token: a bracket by its matching
    bracket, a CASE by END. sqlglot reads an unquoted `end` elsewhere as a
    column name, so an END that closed whatever level was innermost let
    `ARRAY[end, ...` or `(end + ...` open levels the scan forgot at once,
    undercounting every cap here. PostgreSQL reserves `end`: outside a CASE
    it is only valid as a column label, so an END that closes no CASE is
    refused, except right after AS (`max(x) AS end`), where it is a label
    and closes nothing.
    """
    if len(tokens) > limits.tokens:
        raise _too_complex()
    # Per open level: the token that opened it, whether a type keyword
    # opened it, and its depth.
    open_levels: list[tuple[TokenType, bool, int]] = []
    type_nesting = 0
    closed_depth = 0  # depth of the level the previous token closed
    reads = 0
    square_brackets = 0
    previous: TokenType | None = None
    for token in tokens:
        kind = token.token_type
        if kind in _CLOSES:
            by_type = kind in _OPENERS and previous in _TYPE_TOKENS
            depth = (open_levels[-1][2] if open_levels else 0) + 1
            if kind == TokenType.L_BRACKET:
                square_brackets += 1
                if previous == TokenType.R_BRACKET:
                    # `x[1][2]` subscripts the value just closed: the parser
                    # recurses once per link (and walks the chain each
                    # time), so a chain nests like brackets inside brackets.
                    depth = closed_depth + 1
            open_levels.append((kind, by_type, depth))
            type_nesting += by_type
            if (
                depth > limits.depth
                or type_nesting > MAX_TYPE_NESTING
                or square_brackets > limits.square_brackets
            ):
                raise _too_complex()
        elif kind in _LEVEL_CLOSERS and not (
            kind == TokenType.END and previous == TokenType.ALIAS
        ):
            if not open_levels or _CLOSES[open_levels[-1][0]] != kind:
                raise _reject("unparseable")
            _, by_type, closed_depth = open_levels.pop()
            type_nesting -= by_type
        # Read once per level of type-opened brackets around it (an opener
        # counts inside the level it opens, a closer outside the one it
        # closes).
        reads += 1 << type_nesting
        if reads > limits.tokens:
            raise _too_complex()
        previous = kind


def _over_node_budget(error: ParseError) -> bool:
    return any(
        "Maximum number of AST nodes" in str(detail.get("description", ""))
        for detail in error.errors
    )


def _parse_single_statement(sql: str, tokens: list[Token], limits: _Limits) -> exp.Expr:
    # `max_nodes` counts every node the parser builds, including those of
    # attempts it abandons when it backtracks: a budget on its work, not
    # only on the size of the tree it returns.
    parser = _POSTGRES.parser(max_nodes=limits.nodes)
    try:
        parsed = parser.parse(tokens, sql)
    except ParseError as error:
        if _over_node_budget(error):
            raise _too_complex() from error
        raise _reject("unparseable") from error
    except RecursionError as error:
        raise _too_complex() from error
    except Exception as error:  # any parser failure is a refusal
        raise _reject("unparseable") from error
    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise _reject("invalid_input")
    if len(statements) > 1:
        raise _reject("multiple_statements")
    return statements[0]


def _check_size(statement: exp.Expr, limits: _Limits) -> None:
    for count, _ in enumerate(statement.walk(), start=1):
        if count > limits.nodes:
            raise _too_complex()


def _continues_chain(parent: exp.Expr, child: exp.Expr) -> bool:
    """True for the links sqlglot walks iteratively instead of recursively:
    an AND/OR under an AND/OR, or a binary operator under the same one."""
    if isinstance(parent, exp.Connector):
        return isinstance(child, exp.Connector)
    return isinstance(parent, exp.Binary) and type(child) is type(parent)


def _check_tree_depth(statement: exp.Expr, limits: _Limits) -> None:
    """Refuse a tree deeper than the recursive passes after parsing can take.

    Iterative, so it cannot itself run out of stack. A long AND/OR or
    same-operator chain counts as one level: sqlglot renders it in a loop.
    """
    stack = [(statement, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limits.tree_depth:
            raise _too_complex()
        for child in node.iter_expressions():
            stack.append((child, depth if _continues_chain(node, child) else depth + 1))


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
        # No copy: the guard renders each tree once and never uses it again,
        # and copying a tree at the size caps cost more than rendering it.
        text = self.generate(statement, copy=False)
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
    sql: str, policy: QueryPolicy, limits: _Limits
) -> tuple[exp.Select | exp.SetOperation, list[str]]:
    """Parse *sql* within *limits* and run the structural and relation checks.

    Relations come back schema-qualified. Function names and types are
    checked by `_render`, which every accepted query goes through.
    """
    tokens = _tokenize(sql)
    _check_tokens(tokens, limits)
    parsed = _parse_single_statement(sql, tokens, limits)
    _check_size(parsed, limits)
    _check_tree_depth(parsed, limits)
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
        raise _too_complex() from error
    except Exception as error:  # any other failure is a refusal, never a crash
        raise _reject("unparseable") from error


def _guard(sql: str, policy: QueryPolicy, row_limit: int) -> GuardedQuery:
    limits = _Limits.of(policy)
    statement, relations = _validate(sql, policy, limits)
    rendered = _render(statement, policy)

    # Fixed point: the rendering is what executes, so it must itself pass
    # every check and render back to exactly the same text. A renderer that
    # wrote a construct back in a form that reads differently fails here
    # instead of reaching the database with an unchecked meaning.
    try:
        executed, relations = _validate(rendered, policy, limits.for_rendering())
        rerendered = _render(executed, policy)
    except QueryRejectedError as error:
        raise _reject("unparseable") from error
    if rerendered != rendered:
        raise _reject("unparseable")

    # Static text around the validated rendering and an integer: exactly
    # what rendering the wrapped tree produced, without copying and
    # rendering the whole tree a third time.
    final = f'SELECT * FROM ({rendered}) AS "{_RESULT_ALIAS}" LIMIT {row_limit + 1}'
    return GuardedQuery(sql=final, relations=tuple(sorted(set(relations))))
