"""No runtime-id parsing is left in the package (ADR-004 PR4b).

spec (agent-registration-serving): "No duplicated runtime-id parsing logic
remains" -- a runtime id is an opaque registration key everywhere it is used.
The `{deployment}__{role}` scheme once had two independent parsers
(`openai_adapter.parse_model_id` and an inline `model_id.split("__", 1)` in
`main.py`), and a `_generic` sentinel for "no deployment". This is the
regression gate against either coming back, under any name: it reads every
module under `src/agents_system/` and fails on any code that splits,
partitions, builds or tests a string around a `"__"` separator, or names the
`"_generic"` sentinel.

`AGENT_REGISTRATIONS` values are parsed by `main._parse_agent_registration`
on `"@"`, never `"__"`, and never out of a runtime id.

The same gate covers prose: no module, and neither twin of the doc that
explains how the lifespan labels a runtime's metrics, may still describe the
scheme as current behavior.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

import agents_system

_PACKAGE_ROOT = pathlib.Path(agents_system.__file__).resolve().parent
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SEPARATOR = "__"
_SENTINEL = "_generic"
_SPLITTERS = frozenset({"split", "rsplit", "partition", "rpartition"})
_SCHEME = "{deployment}__{role}"
#: A dunder name (`__init__`, `__tablename__`, ...): legitimate Python
#: identifier convention, never a `{deployment}__{role}`-style separator use.
_DUNDER_NAME = re.compile(r"^__\w+__$")


def _is_separator(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == _SEPARATOR


def _docstring_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every `Constant` node that is a module/class/function
    docstring: its own scope's first statement, a bare string expression."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _runtime_id_parsing(source: str) -> list[str]:
    """Every place in *source* that treats `"__"` as a separator, names the
    `"_generic"` sentinel, or otherwise embeds `"__"` in a string constant
    that is not a docstring, a dunder name, or the allow-listed migration
    hint (`_SCHEME`) -- as `"<line>: <what>"` (issue #128 item 7).

    The specific shapes below (a splitter call, a `.join()` call, an `in`/
    `not in` comparison, an f-string segment, the `_generic` sentinel) give a
    readable message for the exact patterns this scheme's removal (ADR-004
    PR4b) actually used. The final, generic check is the real regression
    gate: ANY other non-dunder, non-docstring string constant containing
    `"__"` -- a `str.split(x, "__")` with the separator past `args[0]`, a
    `"__".join(...)` (the separator is the call's RECEIVER, never an
    argument), an f-string segment that merely CONTAINS `"__"` rather than
    being exactly `"__"`, a `re.fullmatch`/`re.compile` pattern, or any other
    shape a reintroduced parser could take -- is still flagged even when it
    matches none of the specific shapes above.
    """
    found: list[str] = []
    tree = ast.parse(source)
    docstring_ids = _docstring_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SPLITTERS
            and any(_is_separator(arg) for arg in node.args)
        ):
            found.append(f"{node.lineno}: .{node.func.attr}({_SEPARATOR!r})")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and _is_separator(node.func.value)
        ):
            found.append(f"{node.lineno}: {_SEPARATOR!r}.join(...)")
        elif (
            isinstance(node, ast.Compare)
            and _is_separator(node.left)
            and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
        ):
            found.append(f"{node.lineno}: {_SEPARATOR!r} in ...")
        elif isinstance(node, ast.JoinedStr) and any(
            _is_separator(part) for part in node.values
        ):
            found.append(f'{node.lineno}: f"{{...}}{_SEPARATOR}{{...}}"')
        elif isinstance(node, ast.Constant) and node.value == _SENTINEL:
            found.append(f"{node.lineno}: {_SENTINEL!r}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SEPARATOR in node.value
            and not _DUNDER_NAME.fullmatch(node.value)
            and id(node) not in docstring_ids
            and _SCHEME not in node.value
        ):
            found.append(
                f"{node.lineno}: string constant containing "
                f"{_SEPARATOR!r}: {node.value!r}"
            )
    return found


# The deleted helpers, verbatim in substance: the scan must flag every one of
# their moves, or passing it would prove nothing.
_REMOVED_SCHEME = """
def to_model_id(role, deployment):
    prefix = deployment if deployment is not None else "_generic"
    return f"{prefix}__{role}"


def parse_model_id(model_id):
    if "__" not in model_id:
        raise ValueError(model_id)
    prefix, role = model_id.split("__", 1)
    return (None if prefix == "_generic" else prefix), role
"""


def test_the_scan_flags_every_move_of_the_removed_scheme() -> None:
    found = _runtime_id_parsing(_REMOVED_SCHEME)

    assert "3: '_generic'" in found
    assert '4: f"{...}__{...}"' in found
    assert "8: '__' in ..." in found
    assert "10: .split('__')" in found
    assert "11: '_generic'" in found


@pytest.mark.parametrize(
    "source",
    [
        'deployment, _, role = model_id.partition("__")',
        'role = model_id.rsplit("__", 1)[-1]',
        'is_legacy = "__" in model_id',
        # issue #128 item 7 -- these previously passed undetected: the scan
        # only checked a splitter call's FIRST argument, only a `.join()`
        # call's ARGUMENT (never its receiver), and only an f-string segment
        # that was EXACTLY "__" (never one merely containing it).
        'role = str.split(model_id, "__")[0]',  # separator not args[0]
        'model_id = "__".join([deployment, role])',  # "__" is the receiver
        'model_id = f"{deployment}__{role}"',  # same shape, unbound var names
        'legacy_id = f"prefix__{role}"',  # "__" embedded in a larger literal
        'PATTERN = re.compile(r"(?P<deployment>.+)__(?P<role>.+)")',  # regex
    ],
)
def test_the_scan_flags_a_renamed_successor(source: str) -> None:
    assert _runtime_id_parsing(source)


# ---------------------------------------------------------------------------
# issue #128 item 7 -- the broadened generic-constant check must not flag a
# docstring, a real dunder name, or the one allow-listed migration-hint
# string (`_SCHEME`, used verbatim in `main.py`'s unregistered-channel error
# to tell an operator what the OLD scheme looked like).
# ---------------------------------------------------------------------------


def test_the_scan_does_not_flag_a_module_docstring_mentioning_the_separator() -> None:
    source = '"""Explains the old `{deployment}__{role}` scheme, historically."""\n'
    assert _runtime_id_parsing(source) == []


def test_the_scan_does_not_flag_a_function_docstring_mentioning_the_separator() -> None:
    source = (
        "def f():\n"
        '    """Uses `__` as a separator, historically -- see module docs."""\n'
        "    return 1\n"
    )
    assert _runtime_id_parsing(source) == []


def test_the_scan_does_not_flag_a_real_dunder_name() -> None:
    source = 'name = "__tablename__"\n'
    assert _runtime_id_parsing(source) == []


def test_the_scan_does_not_flag_the_allow_listed_migration_hint() -> None:
    source = (
        "hint = \" is no longer read as '{deployment}__{role}': map it in "
        'AGENT_REGISTRATIONS"\n'
    )
    assert _runtime_id_parsing(source) == []


def test_the_scan_flags_an_arbitrary_non_docstring_constant_containing_the_separator() -> (
    None
):
    """The generic catch-all: any other non-dunder, non-docstring string
    constant containing `__` -- not just the four specific shapes above."""
    source = 'DEBUG_LABEL = "role__client debug label"\n'
    assert _runtime_id_parsing(source)


def test_no_module_parses_a_runtime_id() -> None:
    modules = sorted(_PACKAGE_ROOT.rglob("*.py"))
    assert modules, f"no modules found under {_PACKAGE_ROOT}"

    offenders = {
        str(module.relative_to(_PACKAGE_ROOT)): found
        for module in modules
        if (found := _runtime_id_parsing(module.read_text(encoding="utf-8")))
    }

    assert offenders == {}, (
        "A runtime id is an opaque registration key (ADR-004): nothing may "
        f"split it on {_SEPARATOR!r} or recognise a {_SENTINEL!r} sentinel. "
        f"Found: {offenders}"
    )


# The docs that say which id the lifespan passes as a runtime's metrics label,
# EN and its ES twin.
_RUNTIME_ID_DOCS = (
    "docs/platform/observability.md",
    "docs/platform_es/observability.md",
)

# The only modules that may still name the removed scheme, and how often.
# Each tells a reader what replaced it; none describes it as current behavior.
_ALLOWED_SCHEME_MENTIONS = {
    # The unregistered-channel error's static migration hint.
    "main.py": 1,
    # The AGENT_REGISTRATIONS comment, naming the encoding it replaces.
    "config.py": 1,
}


def test_no_module_or_runtime_id_doc_describes_the_removed_scheme() -> None:
    mentions = {
        str(module.relative_to(_PACKAGE_ROOT)): count
        for module in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if (count := module.read_text(encoding="utf-8").count(_SCHEME))
    }
    mentions |= {
        doc: count
        for doc in _RUNTIME_ID_DOCS
        if (count := (_REPO_ROOT / doc).read_text(encoding="utf-8").count(_SCHEME))
    }

    assert mentions == _ALLOWED_SCHEME_MENTIONS, (
        f"The {_SCHEME!r} runtime-id scheme is removed (ADR-004): a runtime "
        "id is the opaque key from create_app(agents=...) or "
        "AGENT_REGISTRATIONS. Only a migration hint may name the old scheme. "
        f"Found: {mentions}"
    )


@pytest.mark.parametrize("doc", _RUNTIME_ID_DOCS)
def test_runtime_id_doc_names_where_the_registered_id_comes_from(doc: str) -> None:
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")

    assert "create_app(agents=...)" in text
    assert "AGENT_REGISTRATIONS" in text
