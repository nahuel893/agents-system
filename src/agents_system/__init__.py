"""agents_system — a reusable AI agent platform library.

``agents_system`` ships the GENERIC harness and the GENERIC platform agent roles
(``platform/roles/``). A client application supplies its own context on top:
its own tools (wired to its own services), and — optionally — its own
deployment overrides (``deployments/{client}/``) with client-specific prompts,
skills, and policy restrictions.

The injection flow, end to end::

    import agents_system

    # 1. Build your own registry and register your own ToolSpec(s). The
    #    connector is YOUR code — it can call your database, your APIs,
    #    anything. `catalog_search` here just has to match a tool name a
    #    platform role manifest declares (see platform/roles/*/manifest.md).
    registry = agents_system.ToolRegistry()
    registry.register(
        agents_system.ToolSpec(
            name="catalog_search",
            required_permissions=("read:catalog",),
            connector=my_catalog_search_connector,
            tier=agents_system.Tier.T1,  # ADR-002 C.10 — required, no default
        )
    )

    # 2. Point RootConfig at YOUR deployments tree (optional — omit `client`
    #    below to use the generic role as-is with no deployment override).
    roots = agents_system.RootConfig(deployments_root=MY_APP_DEPLOYMENTS_DIR)

    # 3. Build the runtime: role + your registry + the permissions this
    #    caller/identity currently holds. This call is the ONLY path to a
    #    grant — there is no auto-grant anywhere in this library flow, and
    #    `granted_permissions` becomes EquippedRuntime.deploy_grant_ceiling
    #    (issue #38), the bound Layer-2 revalidation enforces. A deployment
    #    booting through `agents_system.main` instead configures the
    #    equivalent explicit grant via `DEPLOY_GRANTS` (see the
    #    permission-model ADR).
    equipped = agents_system.build_runtime(
        "sales-agent",
        registry,
        granted_permissions=["read:catalog"],
        client="my-client",  # optional
        roots=roots,
    )

    # 4. Turn it into a live, running agent with any LangChain chat model.
    agent = agents_system.AgentRuntime(equipped, model=my_chat_model)
    reply = await agent.run_turn(messages, session_id="s1")

This module is populated lazily (PEP 562 module ``__getattr__``): importing
``agents_system`` does not import FastAPI, LangGraph, or any other heavy
submodule — those are only imported the first time you actually touch the
corresponding name (e.g. ``agents_system.AgentRuntime`` imports ``agent.graph``,
and only then, pulling in LangGraph as a side effect).
"""

from __future__ import annotations

import importlib
from importlib import metadata as _metadata
from typing import Any

# Public name -> (dotted module path, attribute name in that module).
# Keep this the single source of truth for the package's public surface.
_EXPORTS: dict[str, tuple[str, str]] = {
    "ToolRegistry": ("agents_system.harness.registry", "ToolRegistry"),
    "ToolSpec": ("agents_system.harness.registry", "ToolSpec"),
    "Tier": ("agents_system.harness.registry", "Tier"),
    "ToolNotFoundError": ("agents_system.harness.registry", "ToolNotFoundError"),
    "RootConfig": ("agents_system.harness.loader", "RootConfig"),
    "AgentDefinition": ("agents_system.harness.loader", "AgentDefinition"),
    "resolve": ("agents_system.harness.loader", "resolve"),
    "DefinitionError": ("agents_system.harness.loader", "DefinitionError"),
    "build_runtime": ("agents_system.harness.factory", "build_runtime"),
    "EquippedRuntime": ("agents_system.harness.factory", "EquippedRuntime"),
    "FactoryError": ("agents_system.harness.factory", "FactoryError"),
    "InjectionError": ("agents_system.harness.injector", "InjectionError"),
    "AgentRuntime": ("agents_system.agent.graph", "AgentRuntime"),
}

# PLE0605/PLE0604 false positive: ruff's static check cannot see that
# sorted() over this dict's str keys, plus a literal str, is a list[str].
__all__ = sorted(_EXPORTS) + ["__version__"]  # noqa: PLE0605


def _resolve_version() -> str:
    """Read the installed distribution version; fall back sensibly when the
    package metadata isn't available (e.g. a source checkout not installed
    via pip/uv)."""
    try:
        return _metadata.version("agents-system")
    except _metadata.PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolution for the names in ``_EXPORTS``.

    Only imports the owning submodule the first time a given name is
    actually accessed, then caches the result on this module so repeated
    access is a plain attribute lookup (no repeated importlib call).
    """
    try:
        module_path, attr_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    module = importlib.import_module(module_path)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """The public surface, and only that.

    Not `set(__all__) | set(globals())`: this module's globals hold its own
    imports (`importlib`, `Any`, `annotations`, `_metadata`, `_EXPORTS`), and
    the union advertised them as exports. `from agents_system import *` was never
    affected — it honours `__all__` — but `dir()` is what autocomplete,
    `help()` and doc tooling read, and defining the public surface is this
    module's entire job.

    The union was also unstable: `__getattr__` caches each resolved export
    into `globals()`, so the answer depended on which attributes had already
    been touched.
    """
    return sorted(__all__)
