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
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import agents_system

_PACKAGE_ROOT = pathlib.Path(agents_system.__file__).resolve().parent
_SEPARATOR = "__"
_SENTINEL = "_generic"
_SPLITTERS = frozenset({"split", "rsplit", "partition", "rpartition"})


def _is_separator(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == _SEPARATOR


def _runtime_id_parsing(source: str) -> list[str]:
    """Every place in *source* that treats `"__"` as a separator, or names the
    `"_generic"` sentinel, as `"<line>: <what>"`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SPLITTERS
            and node.args
            and _is_separator(node.args[0])
        ):
            found.append(f"{node.lineno}: .{node.func.attr}({_SEPARATOR!r})")
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
    ],
)
def test_the_scan_flags_a_renamed_successor(source: str) -> None:
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
