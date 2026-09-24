"""Tests for the package's public API (src/agents_system/__init__.py) — D-024.

`agents_system` ships as an importable library: a client application builds its
own `ToolRegistry`, registers its own `ToolSpec`s, and calls `build_runtime`
to get an `EquippedRuntime`. Before this change `src/agents_system/__init__.py`
was empty (0 lines) — none of that was reachable via `import agents_system`.

Strict TDD: written before `__init__.py` is populated. Two of these tests
(the subprocess "does not eagerly import X" tests) are written to survive a
*naive* eager-import implementation failing them — see the D-024 report for
the RED run captured against that naive draft.
"""

from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent

# name -> dotted module path that owns the real object
_EXPECTED_EXPORTS: dict[str, str] = {
    "ToolRegistry": "agents_system.harness.registry",
    "ToolSpec": "agents_system.harness.registry",
    "Tier": "agents_system.harness.registry",
    "ToolNotFoundError": "agents_system.harness.registry",
    "RootConfig": "agents_system.harness.loader",
    "AgentDefinition": "agents_system.harness.loader",
    "resolve": "agents_system.harness.loader",
    "DefinitionError": "agents_system.harness.loader",
    "build_runtime": "agents_system.harness.factory",
    "EquippedRuntime": "agents_system.harness.factory",
    "FactoryError": "agents_system.harness.factory",
    "InjectionError": "agents_system.harness.injector",
    "AgentRuntime": "agents_system.agent.graph",
}


# ---------------------------------------------------------------------------
# __all__ reachability — every declared export resolves to the real object
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,module_path",
    sorted(_EXPECTED_EXPORTS.items()),
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_export_is_reachable_and_is_the_real_object(
    name: str, module_path: str
) -> None:
    import agents_system

    real_module = importlib.import_module(module_path)

    exported = getattr(agents_system, name)
    real = getattr(real_module, name)

    assert exported is real


def test_all_declares_every_expected_export_plus_version() -> None:
    import agents_system

    assert set(agents_system.__all__) == set(_EXPECTED_EXPORTS) | {"__version__"}


def test_version_is_a_nonempty_string() -> None:
    import agents_system

    assert isinstance(agents_system.__version__, str)
    assert agents_system.__version__ != ""


# ---------------------------------------------------------------------------
# Unknown attribute access → AttributeError, standard message shape
# ---------------------------------------------------------------------------
def test_unknown_attribute_raises_attribute_error() -> None:
    import agents_system

    with pytest.raises(
        AttributeError, match=r"module 'agents_system' has no attribute 'DoesNotExist'"
    ):
        agents_system.DoesNotExist  # type: ignore[attr-defined]


def test_unknown_attribute_is_not_key_error_or_import_error() -> None:
    """The lazy __getattr__ resolves via a dict lookup and importlib — a
    naive implementation could easily leak a KeyError or ImportError instead
    of AttributeError for an unknown name."""
    import agents_system

    try:
        agents_system.TotallyMadeUpName  # type: ignore[attr-defined]
    except AttributeError:
        pass
    except Exception as exc:  # pragma: no cover - failure path
        pytest.fail(f"expected AttributeError, got {type(exc).__name__}: {exc}")
    else:  # pragma: no cover - failure path
        pytest.fail("expected AttributeError, no exception was raised")


# ---------------------------------------------------------------------------
# __dir__ includes every declared export
# ---------------------------------------------------------------------------
def test_dir_includes_all_exports() -> None:
    import agents_system

    names = dir(agents_system)
    for name in agents_system.__all__:
        assert name in names


# ---------------------------------------------------------------------------
# Laziness — import agents_system must not drag in main.py or agent.graph
# ---------------------------------------------------------------------------
def _run_import_probe(probe: str) -> subprocess.CompletedProcess[str]:
    """Run `probe` in a fresh interpreter so no other test's imports can leak
    into sys.modules and make the assertion vacuously pass."""
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )


def test_import_agents_system_does_not_import_main() -> None:
    """`agents_system.main` builds a FastAPI app and validates the `Settings`
    singleton at module scope (`main.py:326 app = create_app()`). A bare
    `import agents_system` must not pay for that.

    THIS ONE MATTERS: run against a naive eager-import `__init__.py` first —
    it must fail for the right reason (agents_system.main present in sys.modules)
    before the lazy implementation is written. See the D-024 report for the
    captured RED output.
    """
    result = _run_import_probe(
        "import sys\n"
        "import agents_system\n"
        "assert 'agents_system.main' not in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_agents_system_does_not_import_agent_graph() -> None:
    """`agents_system.agent.graph` drags in langgraph; `AgentRuntime` must be
    re-exported lazily too."""
    result = _run_import_probe(
        "import sys\n"
        "import agents_system\n"
        "assert 'agents_system.agent.graph' not in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_touching_agent_runtime_name_then_imports_agent_graph() -> None:
    """The flip side of laziness: once a consumer actually touches the name,
    the real module IS imported (this is lazy, not broken)."""
    result = _run_import_probe(
        "import sys\n"
        "import agents_system\n"
        "agents_system.AgentRuntime\n"
        "assert 'agents_system.agent.graph' in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# End-to-end consumer flow — the actual requirement this task exists for:
# "el cliente debe poder armar su propia tool registry e inyectarlo al agente"
# ---------------------------------------------------------------------------
def test_consumer_builds_own_registry_and_tool_comes_back_granted() -> None:
    import agents_system

    # The client builds its own registry and its own ToolSpec — the connector
    # is the client's code, not the platform's. The name matches a tool
    # declared by the real platform sales-agent manifest
    # (platform/roles/sales-agent/manifest.md): catalog_search.
    registry = agents_system.ToolRegistry()

    def my_own_catalog_search(inputs: dict[str, object]) -> dict[str, object]:
        return {"echo": inputs}

    registry.register(
        agents_system.ToolSpec(
            name="catalog_search",
            required_permissions=("read:catalog",),
            connector=my_own_catalog_search,
            tier=agents_system.Tier.T1,
        )
    )
    # The RESOLVED sales-agent declares 6 tools -- its own 5 plus
    # `escalation_notifier`, inherited from `platform/roles/agent`. The
    # injector raises on any declared tool absent from the registry, so a
    # consumer must cover the whole inherited surface, not just the leaf
    # manifest. That is the visible cost of a base role, and it is the right
    # one: an agent with no escalation path fails silently.
    for name, perms in (
        ("message_sender", ("send:message",)),
        ("order_writer", ("write:orders", "write:order_items")),
        ("session_state", ()),
        ("client_lookup", ("read:client_registry",)),
        ("escalation_notifier", ("send:escalation",)),
    ):
        # Mirror the pre-tier write:/send: heuristic (ADR-002 C.10).
        tier = (
            agents_system.Tier.T2
            if any(p.startswith(("write:", "send:")) for p in perms)
            else agents_system.Tier.T1
        )
        registry.register(
            agents_system.ToolSpec(
                name=name,
                required_permissions=perms,
                connector=lambda inputs: {},
                tier=tier,
            )
        )

    # The client points RootConfig at the real platform/ tree explicitly —
    # no reliance on default resolution (that machinery is tested separately)
    # and no client deployment override.
    roots = agents_system.RootConfig(platform_root=REPO_ROOT / "platform")

    runtime = agents_system.build_runtime(
        "sales-agent",
        registry,
        granted_permissions=["read:catalog"],
        roots=roots,
    )

    assert isinstance(runtime, agents_system.EquippedRuntime)
    granted_names = {spec.name for spec in runtime.tools}
    assert "catalog_search" in granted_names


def test_dir_advertises_only_the_public_surface() -> None:
    """`dir(agents_system)` is the public surface, not the module's globals.

    `__dir__` returned `set(__all__) | set(globals())`, so it advertised
    `importlib`, `Any`, `annotations`, `_metadata` and `_EXPORTS` alongside
    the real exports. `from agents_system import *` was always safe — it honours
    `__all__` — but `dir()` is what autocomplete, help() and doc tooling read,
    and this module exists precisely to say what the public surface is.

    Worse, the union grows as the lazy loader runs: `__getattr__` caches each
    resolved export into `globals()`, so what `dir()` reported depended on
    which attributes had already been touched.
    """
    import agents_system

    assert dir(agents_system) == sorted(agents_system.__all__)


def test_dir_is_stable_once_lazy_exports_have_been_resolved() -> None:
    """Touching an export must not change what `dir()` reports.

    Pins the caching half of the bug above: resolving `ToolRegistry` writes it
    into `globals()`, which the old union then surfaced. Same list before and
    after.
    """
    import agents_system

    before = dir(agents_system)
    agents_system.ToolRegistry  # noqa: B018 — resolving it is the point
    agents_system.build_runtime  # noqa: B018
    assert dir(agents_system) == before


def test_private_implementation_names_are_not_advertised() -> None:
    import agents_system

    for leaked in ("importlib", "Any", "annotations", "_metadata", "_EXPORTS"):
        assert leaked not in dir(agents_system), (
            f"{leaked} leaks into the public surface"
        )
