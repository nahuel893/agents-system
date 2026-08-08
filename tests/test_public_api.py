"""Tests for the package's public API (src/agentsys/__init__.py) — D-024.

`agentsys` ships as an importable library: a client application builds its
own `ToolRegistry`, registers its own `ToolSpec`s, and calls `build_runtime`
to get an `EquippedRuntime`. Before this change `src/agentsys/__init__.py`
was empty (0 lines) — none of that was reachable via `import agentsys`.

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
    "ToolRegistry": "agentsys.harness.registry",
    "ToolSpec": "agentsys.harness.registry",
    "ToolNotFoundError": "agentsys.harness.registry",
    "RootConfig": "agentsys.harness.loader",
    "AgentDefinition": "agentsys.harness.loader",
    "resolve": "agentsys.harness.loader",
    "DefinitionError": "agentsys.harness.loader",
    "build_runtime": "agentsys.harness.factory",
    "EquippedRuntime": "agentsys.harness.factory",
    "FactoryError": "agentsys.harness.factory",
    "InjectionError": "agentsys.harness.injector",
    "AgentRuntime": "agentsys.agent.graph",
}


# ---------------------------------------------------------------------------
# __all__ reachability — every declared export resolves to the real object
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,module_path", sorted(_EXPECTED_EXPORTS.items()), ids=lambda v: v if isinstance(v, str) else None
)
def test_export_is_reachable_and_is_the_real_object(name: str, module_path: str) -> None:
    import agentsys

    real_module = importlib.import_module(module_path)

    exported = getattr(agentsys, name)
    real = getattr(real_module, name)

    assert exported is real


def test_all_declares_every_expected_export_plus_version() -> None:
    import agentsys

    assert set(agentsys.__all__) == set(_EXPECTED_EXPORTS) | {"__version__"}


def test_version_is_a_nonempty_string() -> None:
    import agentsys

    assert isinstance(agentsys.__version__, str)
    assert agentsys.__version__ != ""


# ---------------------------------------------------------------------------
# Unknown attribute access → AttributeError, standard message shape
# ---------------------------------------------------------------------------
def test_unknown_attribute_raises_attribute_error() -> None:
    import agentsys

    with pytest.raises(
        AttributeError, match=r"module 'agentsys' has no attribute 'DoesNotExist'"
    ):
        agentsys.DoesNotExist  # type: ignore[attr-defined]


def test_unknown_attribute_is_not_key_error_or_import_error() -> None:
    """The lazy __getattr__ resolves via a dict lookup and importlib — a
    naive implementation could easily leak a KeyError or ImportError instead
    of AttributeError for an unknown name."""
    import agentsys

    try:
        agentsys.TotallyMadeUpName  # type: ignore[attr-defined]
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
    import agentsys

    names = dir(agentsys)
    for name in agentsys.__all__:
        assert name in names


# ---------------------------------------------------------------------------
# Laziness — import agentsys must not drag in main.py or agent.graph
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


def test_import_agentsys_does_not_import_main() -> None:
    """`agentsys.main` builds a FastAPI app and validates the `Settings`
    singleton at module scope (`main.py:326 app = create_app()`). A bare
    `import agentsys` must not pay for that.

    THIS ONE MATTERS: run against a naive eager-import `__init__.py` first —
    it must fail for the right reason (agentsys.main present in sys.modules)
    before the lazy implementation is written. See the D-024 report for the
    captured RED output.
    """
    result = _run_import_probe(
        "import sys\n"
        "import agentsys\n"
        "assert 'agentsys.main' not in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_agentsys_does_not_import_agent_graph() -> None:
    """`agentsys.agent.graph` drags in langgraph; `AgentRuntime` must be
    re-exported lazily too."""
    result = _run_import_probe(
        "import sys\n"
        "import agentsys\n"
        "assert 'agentsys.agent.graph' not in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_touching_agent_runtime_name_then_imports_agent_graph() -> None:
    """The flip side of laziness: once a consumer actually touches the name,
    the real module IS imported (this is lazy, not broken)."""
    result = _run_import_probe(
        "import sys\n"
        "import agentsys\n"
        "agentsys.AgentRuntime\n"
        "assert 'agentsys.agent.graph' in sys.modules, sorted(sys.modules)\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# End-to-end consumer flow — the actual requirement this task exists for:
# "el cliente debe poder armar su propia tool registry e inyectarlo al agente"
# ---------------------------------------------------------------------------
def test_consumer_builds_own_registry_and_tool_comes_back_granted() -> None:
    import agentsys

    # The client builds its own registry and its own ToolSpec — the connector
    # is the client's code, not the platform's. The name matches a tool
    # declared by the real platform sales-agent manifest
    # (platform/roles/sales-agent/manifest.md): catalog_search.
    registry = agentsys.ToolRegistry()

    def my_own_catalog_search(inputs: dict[str, object]) -> dict[str, object]:
        return {"echo": inputs}

    registry.register(
        agentsys.ToolSpec(
            name="catalog_search",
            required_permissions=("read:catalog",),
            connector=my_own_catalog_search,
        )
    )
    # The manifest declares 5 tools; the injector raises on any declared tool
    # absent from the registry, so the client must cover the full surface it
    # intends to grant (it may leave some ungranted via permissions instead).
    for name, perms in (
        ("message_sender", ("send:message",)),
        ("order_writer", ("write:orders", "write:order_items")),
        ("session_state", ()),
        ("client_lookup", ("read:client_registry",)),
    ):
        registry.register(
            agentsys.ToolSpec(
                name=name,
                required_permissions=perms,
                connector=lambda inputs: {},
            )
        )

    # The client points RootConfig at the real platform/ tree explicitly —
    # no reliance on default resolution (that machinery is tested separately)
    # and no client deployment override.
    roots = agentsys.RootConfig(platform_root=REPO_ROOT / "platform")

    runtime = agentsys.build_runtime(
        "sales-agent",
        registry,
        granted_permissions=["read:catalog"],
        roots=roots,
    )

    assert isinstance(runtime, agentsys.EquippedRuntime)
    granted_names = {spec.name for spec in runtime.tools}
    assert "catalog_search" in granted_names
