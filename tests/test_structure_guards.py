"""Structure guards for the module split (#337).

Three checks that read the source tree and never import it:

* a size cap per module, with the modules already over it listed and unable to grow;
* layering contracts between the packages, with today's violations listed;
* a patch-target guard: a test that patches ``module.name`` must name a module
  that defines ``name`` or imports it and uses it. A module that only
  re-exports a name does not look it up, so patching it there changes nothing
  and the test would pass without testing anything.

Every list of exceptions may only shrink: a new entry fails, and so does an
entry that no longer applies.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
PACKAGE = "agents_system"
PACKAGE_ROOT = SRC_ROOT / PACKAGE
TESTS_ROOT = REPO_ROOT / "tests"


# --------------------------------------------------------------------------- #
# Size cap
# --------------------------------------------------------------------------- #

MAX_MODULE_LINES = 500

# Modules already over the cap, each with the ceiling it may not pass. Lower a
# ceiling when a module shrinks, and delete the entry once it fits the cap.
OVERSIZED_MODULES: dict[str, int] = {
    "harness/loader.py": 4100,
    "main.py": 2100,
    "agent/graph.py": 2100,
    "evals/runner.py": 1500,
    "evals/live_harness.py": 1200,
    "connectors/operator.py": 1100,
    "services/sql_guard.py": 1000,
    "schedules.py": 900,
    "services/webhook_worker.py": 800,
    "harness/factory.py": 800,
    "evals/schema.py": 800,
    "config.py": 700,
    "audit/recorder.py": 700,
    "proactive.py": 700,
    "services/outbox.py": 600,
    "connectors/sales_reports.py": 600,
    "services/checkpointer.py": 600,
    "evals/matrix.py": 600,
    "services/reference.py": 600,
}


def _module_sizes(package_root: Path) -> dict[str, int]:
    return {
        path.relative_to(package_root).as_posix(): len(path.read_text().splitlines())
        for path in sorted(package_root.rglob("*.py"))
    }


def test_no_module_passes_the_size_cap_unless_listed() -> None:
    sizes = _module_sizes(PACKAGE_ROOT)
    unlisted = {
        name: lines
        for name, lines in sizes.items()
        if lines > MAX_MODULE_LINES and name not in OVERSIZED_MODULES
    }
    assert not unlisted, (
        f"modules over {MAX_MODULE_LINES} lines; split them instead of listing them: {unlisted}"
    )


def test_listed_oversized_modules_do_not_grow() -> None:
    sizes = _module_sizes(PACKAGE_ROOT)
    grown = {
        name: (sizes[name], ceiling)
        for name, ceiling in OVERSIZED_MODULES.items()
        if sizes.get(name, 0) > ceiling
    }
    assert not grown, f"oversized modules past their ceiling (lines, ceiling): {grown}"


def test_oversized_list_has_no_stale_entry() -> None:
    sizes = _module_sizes(PACKAGE_ROOT)
    stale = sorted(
        name for name in OVERSIZED_MODULES if sizes.get(name, 0) <= MAX_MODULE_LINES
    )
    assert not stale, (
        f"these modules now fit the cap; remove them from the list: {stale}"
    )


# --------------------------------------------------------------------------- #
# Layering contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Contract:
    """Modules whose top-level unit is in ``importers`` may not import ``forbidden``.

    A unit is the first path segment under the package: a subpackage
    (``harness``) or a root module (``main``). ``importers=None`` means every
    unit outside ``exempt``.
    """

    rule: str
    forbidden: frozenset[str]
    importers: frozenset[str] | None = None
    exempt: frozenset[str] = frozenset()

    def applies_to(self, unit: str) -> bool:
        if unit in self.exempt:
            return False
        return self.importers is None or unit in self.importers


_SERVING = frozenset({"main", "server", "integration", "channels"})

CONTRACTS: tuple[Contract, ...] = (
    Contract(
        rule="only the serving layer and the eval harness import the serving layer",
        forbidden=_SERVING,
        exempt=_SERVING | {"evals"},
    ),
    Contract(
        rule="the permission model depends on neither the harness nor the runtime",
        importers=frozenset({"permissions"}),
        forbidden=frozenset({"harness", "agent"}),
    ),
    Contract(
        rule="the runtime and the harness import no connector and no service",
        importers=frozenset({"agent", "harness"}),
        forbidden=frozenset({"connectors", "services"}),
    ),
    Contract(
        rule="settings import no service",
        importers=frozenset({"config"}),
        forbidden=frozenset({"services"}),
    ),
)

# (importing module, imported unit) pairs that break a contract today.
KNOWN_LAYERING_VIOLATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("config.py", "services"),
        ("harness/defaults.py", "connectors"),
        ("harness/injector.py", "connectors"),
        ("permissions/base.py", "harness"),
        ("permissions/builtins.py", "harness"),
        ("permissions/permission_registry.py", "harness"),
        ("services/webhook_worker.py", "channels"),
    }
)


def _imported_modules(tree: ast.AST, module_parts: tuple[str, ...]) -> Iterator[str]:
    """Yield every absolute dotted module a tree imports, deferred imports included."""
    package_parts = (PACKAGE, *module_parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                prefix = (
                    ".".join((*base, node.module)) if node.module else ".".join(base)
                )
            else:
                prefix = node.module or ""
            yield prefix
            # ``from package import submodule`` names the submodule in the alias.
            for alias in node.names:
                yield f"{prefix}.{alias.name}"


def _unit_edges(package_root: Path) -> set[tuple[str, str]]:
    """Return (importing module, imported unit) for every import across units."""
    units = {path.stem for path in package_root.glob("*.py")} | {
        path.name for path in package_root.iterdir() if (path / "__init__.py").is_file()
    }
    edges: set[tuple[str, str]] = set()
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        module_parts = relative.with_suffix("").parts
        own_unit = module_parts[0]
        for dotted in _imported_modules(ast.parse(path.read_text()), module_parts):
            parts = dotted.split(".")
            if len(parts) < 2 or parts[0] != PACKAGE:
                continue
            target = parts[1]
            if target in units and target != own_unit:
                edges.add((relative.as_posix(), target))
    return edges


def _layering_violations(package_root: Path) -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    for module, target in _unit_edges(package_root):
        unit = Path(module).with_suffix("").parts[0]
        if any(c.applies_to(unit) and target in c.forbidden for c in CONTRACTS):
            violations.add((module, target))
    return violations


def test_no_new_layering_violation() -> None:
    new = sorted(_layering_violations(PACKAGE_ROOT) - KNOWN_LAYERING_VIOLATIONS)
    rules = "; ".join(contract.rule for contract in CONTRACTS)
    assert not new, f"imports that break a layering contract ({rules}): {new}"


def test_layering_violation_list_has_no_stale_entry() -> None:
    stale = sorted(KNOWN_LAYERING_VIOLATIONS - _layering_violations(PACKAGE_ROOT))
    assert not stale, f"these imports are gone; remove them from the list: {stale}"


# --------------------------------------------------------------------------- #
# Patch targets
# --------------------------------------------------------------------------- #

DEFINED = "defined"
IMPORTED_AND_USED = "imported and used"
REEXPORTED_ONLY = "imported but never used there (a re-export)"
MISSING = "not bound there"

# (test file, "module.name") patches that name a re-export or an unbound name today.
KNOWN_BAD_PATCH_TARGETS: frozenset[tuple[str, str]] = frozenset()


def _module_file(dotted: str, src_root: Path) -> Path | None:
    base = src_root.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_level_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements that run at import time, looking inside ``if``/``try``/``with``."""
    for statement in body:
        yield statement
        if isinstance(statement, ast.If):
            yield from _module_level_statements(statement.body)
            yield from _module_level_statements(statement.orelse)
        elif isinstance(statement, ast.Try):
            yield from _module_level_statements(statement.body)
            for handler in statement.handlers:
                yield from _module_level_statements(handler.body)
            yield from _module_level_statements(statement.orelse)
            yield from _module_level_statements(statement.finalbody)
        elif isinstance(statement, ast.With):
            yield from _module_level_statements(statement.body)


def _assigned_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _assigned_names(element)


def binding_of(name: str, module_source: str) -> str:
    """Say how a module binds ``name``: defined, imported and used, re-exported, or missing."""
    tree = ast.parse(module_source)
    defined: set[str] = set()
    imported: set[str] = set()
    for statement in _module_level_statements(tree.body):
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined.add(statement.name)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                defined.update(_assigned_names(target))
        elif isinstance(statement, ast.AnnAssign | ast.AugAssign):
            defined.update(_assigned_names(statement.target))
        elif isinstance(statement, ast.Import):
            imported.update(a.asname or a.name.split(".")[0] for a in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            imported.update(a.asname or a.name for a in statement.names)
    if name in defined:
        return DEFINED
    if name not in imported:
        return MISSING
    used = any(
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )
    return IMPORTED_AND_USED if used else REEXPORTED_ONLY


def _module_aliases(tree: ast.AST, src_root: Path) -> dict[str, str]:
    """Map the names a test file binds to package modules onto their dotted paths."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and _module_file(alias.name, src_root):
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted.startswith(f"{PACKAGE}.") and _module_file(dotted, src_root):
                    aliases[alias.asname or alias.name] = dotted
    return aliases


def _split_dotted_target(dotted: str, src_root: Path) -> tuple[str, str] | None:
    """Split ``pkg.module.name.attr`` into the longest real module and the name after it."""
    parts = dotted.split(".")
    for end in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:end])
        if _module_file(module, src_root):
            return module, parts[end]
    return None


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def patch_sites(test_source: str, src_root: Path) -> list[tuple[int, str, str]]:
    """Return (line, module, name) for every patch of a name on a package module.

    Covers ``patch("pkg.mod.name")``, ``monkeypatch.setattr("pkg.mod.name", v)``,
    ``monkeypatch.setattr(mod, "name", v)`` and ``patch.object(mod, "name")``,
    where ``mod`` is a name the file imported as a package module.
    """
    tree = ast.parse(test_source)
    aliases = _module_aliases(tree, src_root)
    sites: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if _called_name(node) not in {"patch", "setattr", "delattr", "object"}:
            continue
        first = node.args[0]
        dotted = _string(first)
        if dotted is not None:
            if dotted.startswith(f"{PACKAGE}."):
                split = _split_dotted_target(dotted, src_root)
                if split:
                    sites.append((node.lineno, *split))
            continue
        name = _string(node.args[1]) if len(node.args) > 1 else None
        if name is None:
            continue
        expression = ast.unparse(first)
        module = aliases.get(expression)
        if module is None and _module_file(expression, src_root):
            module = expression
        if module and module.startswith(PACKAGE):
            sites.append((node.lineno, module, name.split(".")[0]))
    return sites


def _bad_patch_targets(
    tests_root: Path, src_root: Path
) -> dict[tuple[str, str], list[str]]:
    bad: dict[tuple[str, str], list[str]] = {}
    sources: dict[str, str] = {}
    for path in sorted(tests_root.rglob("*.py")):
        relative = path.relative_to(tests_root).as_posix()
        for line, module, name in patch_sites(path.read_text(), src_root):
            if module not in sources:
                module_file = _module_file(module, src_root)
                assert module_file is not None
                sources[module] = module_file.read_text()
            verdict = binding_of(name, sources[module])
            if verdict in {REEXPORTED_ONLY, MISSING}:
                bad.setdefault((relative, f"{module}.{name}"), []).append(
                    f"{relative}:{line} patches {module}.{name}, which is {verdict}"
                )
    return bad


def test_no_new_patch_of_a_reexported_or_unbound_name() -> None:
    bad = _bad_patch_targets(TESTS_ROOT, SRC_ROOT)
    new = [
        line
        for key, lines in sorted(bad.items())
        if key not in KNOWN_BAD_PATCH_TARGETS
        for line in lines
    ]
    assert not new, (
        "patch the module that looks the name up, not one that re-exports it:\n"
        + "\n".join(new)
    )


def test_bad_patch_target_list_has_no_stale_entry() -> None:
    stale = sorted(
        KNOWN_BAD_PATCH_TARGETS - set(_bad_patch_targets(TESTS_ROOT, SRC_ROOT))
    )
    assert not stale, f"these patches are fixed; remove them from the list: {stale}"


# --------------------------------------------------------------------------- #
# The guards themselves: each must fail on the case it exists to catch
# --------------------------------------------------------------------------- #


def test_binding_of_tells_a_reexport_from_a_real_lookup() -> None:
    source = (
        "import os\n"
        "from pkg.impl import moved, helper\n"
        "from pkg import other as renamed\n"
        "LIMIT: int = 3\n"
        "try:\n"
        "    import fast\n"
        "except ImportError:\n"
        "    fast = None\n"
        "def local():\n"
        "    return helper() + renamed.value\n"
        "__all__ = ['moved']\n"
    )
    assert binding_of("local", source) == DEFINED
    assert binding_of("LIMIT", source) == DEFINED
    assert binding_of("fast", source) == DEFINED
    assert binding_of("helper", source) == IMPORTED_AND_USED
    assert binding_of("renamed", source) == IMPORTED_AND_USED
    assert binding_of("moved", source) == REEXPORTED_ONLY
    assert binding_of("os", source) == REEXPORTED_ONLY
    assert binding_of("absent", source) == MISSING


def _write_package(src_root: Path) -> None:
    package = src_root / PACKAGE
    (package / "impl").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "impl" / "__init__.py").write_text("")
    (package / "impl" / "core.py").write_text("def work():\n    return 1\n")
    (package / "facade.py").write_text(f"from {PACKAGE}.impl.core import work\n")


def test_patch_sites_finds_every_supported_form(tmp_path: Path) -> None:
    _write_package(tmp_path)
    test_source = (
        f"import {PACKAGE}.facade as facade_module\n"
        f"from {PACKAGE}.impl import core\n"
        "from unittest.mock import patch\n"
        "def test_it(monkeypatch):\n"
        f"    monkeypatch.setattr('{PACKAGE}.facade.work', lambda: 2)\n"
        "    monkeypatch.setattr(facade_module, 'work', lambda: 2)\n"
        f"    with patch('{PACKAGE}.impl.core.work'):\n"
        "        pass\n"
        "    with patch.object(core, 'work'):\n"
        "        pass\n"
        "    monkeypatch.setattr(object(), 'work', 1)\n"
        "    monkeypatch.setattr('os.getcwd', lambda: '.')\n"
    )
    assert patch_sites(test_source, tmp_path) == [
        (5, f"{PACKAGE}.facade", "work"),
        (6, f"{PACKAGE}.facade", "work"),
        (7, f"{PACKAGE}.impl.core", "work"),
        (9, f"{PACKAGE}.impl.core", "work"),
    ]


def test_patch_guard_flags_a_patch_on_a_reexporting_module(tmp_path: Path) -> None:
    _write_package(tmp_path)
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_moved.py").write_text(
        "def test_it(monkeypatch):\n"
        f"    monkeypatch.setattr('{PACKAGE}.facade.work', lambda: 2)\n"
        f"    monkeypatch.setattr('{PACKAGE}.impl.core.work', lambda: 2)\n"
        f"    monkeypatch.setattr('{PACKAGE}.impl.core.gone', lambda: 2)\n"
    )
    bad = _bad_patch_targets(tests_root, tmp_path)
    assert set(bad) == {
        ("test_moved.py", f"{PACKAGE}.facade.work"),
        ("test_moved.py", f"{PACKAGE}.impl.core.gone"),
    }


def test_layering_guard_flags_a_forbidden_import(tmp_path: Path) -> None:
    package = tmp_path / PACKAGE
    for unit in ("permissions", "harness", "connectors"):
        (package / unit).mkdir(parents=True)
        (package / unit / "__init__.py").write_text("")
    (package / "main.py").write_text("from .harness import loader\n")
    (package / "permissions" / "base.py").write_text(
        "from ..harness.registry import Tier\n"
    )
    (package / "harness" / "registry.py").write_text(
        "def late():\n    from agents_system.connectors import thing\n    return thing\n"
    )
    (package / "harness" / "loader.py").write_text("from . import registry\n")
    (package / "connectors" / "thing.py").write_text("import agents_system.main\n")
    assert _layering_violations(package) == {
        ("permissions/base.py", "harness"),
        ("harness/registry.py", "connectors"),
        ("connectors/thing.py", "main"),
    }
