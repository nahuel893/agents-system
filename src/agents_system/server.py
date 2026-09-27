"""Production server entrypoint for agents_system.

Provides the ASGI application instance (``app``) and factory (``get_application``)
for production ASGI servers (such as Uvicorn or Gunicorn).

Registry resolution:
    In production, a deployment typically wires its own connectors to real systems.
    Configure ``REGISTRY_FACTORY`` with an import path (e.g. ``my_pkg.connectors:build_registry``).
    When ``REGISTRY_FACTORY`` is unset, ``default_registry_factory`` is used. If registered
    agents are configured (via ``AGENT_REGISTRATIONS`` or ``create_app(agents=...)``), boot
    fails with a ``DefinitionError`` explaining that deployments must supply a registry factory
    providing every tool their agents declare (see ``examples/demo/app.py`` for reference).
    Booting with zero registered agents succeeds.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from fastapi import FastAPI

from agents_system.connectors.order_connector import build_order_writer_tool_spec
from agents_system.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agents_system.connectors.report_connector import build_report_tool_spec
from agents_system.connectors.sales_reports import CATALOG
from agents_system.harness.registry import RegistryFactory, ToolRegistry
from agents_system.main import create_app


def default_registry_factory(
    settings: Any = None,
    embedder: Any = None,
    bi_engine: Any = None,
) -> ToolRegistry:
    """Build a default ToolRegistry with standard platform connectors.

    Connectors operate in fail-closed / unbound mode unless overridden by a
    custom registry factory or explicitly bound engine.

    When invoked during application boot with registered agents, this default
    factory fails closed with a DefinitionError because it cannot equip
    role-specific tools (such as catalog_search or client_lookup). Deployments
    must configure REGISTRY_FACTORY to supply a registry providing every tool
    their agents declare.
    """
    registry = ToolRegistry()
    registry.register(build_knowledge_retrieval_tool_spec(None))
    registry.register(build_conversation_summarizer_tool_spec(None))
    registry.register(build_escalation_notifier_tool_spec(None))
    registry.register(build_order_writer_tool_spec(None))
    registry.register(build_report_tool_spec(bi_engine, CATALOG))

    if settings is not None:
        from agents_system.harness.loader import DefinitionError, resolve

        missing_tools: set[str] = set()
        agent_registrations = getattr(settings, "agent_registrations", None) or {}
        for reg_str in agent_registrations.values():
            role_name = reg_str.partition("@")[0].strip()
            try:
                defn = resolve(role_name)
                for tool_name in defn.tools:
                    if tool_name not in registry:
                        missing_tools.add(tool_name)
            except DefinitionError:
                continue

        missing_str = (
            f" Missing tool(s): {', '.join(sorted(missing_tools))}."
            if missing_tools
            else ""
        )
        raise DefinitionError(
            "REGISTRY_FACTORY is not set, but agent registrations are non-empty. "
            "Deployments must supply a registry factory providing every tool their agents declare."
            f"{missing_str} See examples/demo/app.py for reference."
        )

    return registry


def get_application() -> FastAPI:
    """Construct the FastAPI application using configured settings and registry factory.

    Resolves the registry factory from the ``REGISTRY_FACTORY`` environment variable
    (format: ``module.path:callable_name``). If unset, falls back to
    ``default_registry_factory``.
    """
    factory_path = os.getenv("REGISTRY_FACTORY", "").strip()
    if factory_path:
        module_name, sep, attr_name = factory_path.partition(":")
        if not sep or not module_name or not attr_name:
            raise ValueError(
                f"Invalid REGISTRY_FACTORY {factory_path!r}: expected 'module:callable'"
            )
        module = importlib.import_module(module_name)
        factory: RegistryFactory = getattr(module, attr_name)
        if not callable(factory):
            raise TypeError(
                f"Invalid REGISTRY_FACTORY {factory_path!r}: resolved attribute is not callable"
            )
    else:
        factory = default_registry_factory

    return create_app(registry_factory=factory)


def __getattr__(name: str) -> Any:
    """Lazily instantiate ``app`` so module import does not trigger eager initialization."""
    if name == "app":
        return get_application()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
