from __future__ import annotations

import pytest

from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec


def _connector() -> str:
    return "ok"


def _definition(*, tools: tuple[str, ...], permissions: tuple[str, ...]) -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="prompt",
        tools=tools,
        skills=(),
        context={},
        permissions=permissions,
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


def test_resolve_tool_surface_grants_all_tools_when_permissions_present() -> None:
    from agentsys.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    catalog_search = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
    )
    order_writer = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_connector,
    )
    registry.register(catalog_search)
    registry.register(order_writer)
    definition = _definition(
        tools=("catalog_search", "order_writer"),
        permissions=("read:catalog", "write:orders"),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:catalog", "write:orders"),
    )

    assert result.granted == (catalog_search, order_writer)
    assert result.denied == ()


def test_resolve_tool_surface_denies_tool_with_missing_permissions() -> None:
    from agentsys.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    catalog_search = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
    )
    order_writer = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders", "write:order_items"),
        connector=_connector,
    )
    registry.register(catalog_search)
    registry.register(order_writer)
    definition = _definition(
        tools=("catalog_search", "order_writer"),
        permissions=("read:catalog", "write:orders", "write:order_items"),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:catalog", "write:orders"),
    )

    assert result.granted == (catalog_search,)
    assert result.denied == (
        ("order_writer", "missing permissions: write:order_items"),
    )


def test_resolve_tool_surface_uses_role_and_user_permission_intersection() -> None:
    from agentsys.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    client_lookup = ToolSpec(
        name="client_lookup",
        required_permissions=("read:client_registry",),
        connector=_connector,
    )
    registry.register(client_lookup)
    definition = _definition(
        tools=("client_lookup",),
        permissions=("read:client_registry",),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=(),
    )

    assert result.granted == ()
    assert result.denied == (
        ("client_lookup", "missing permissions: read:client_registry"),
    )


def test_resolve_tool_surface_raises_for_unregistered_tool() -> None:
    from agentsys.harness.injector import InjectionError, resolve_tool_surface

    definition = _definition(
        tools=("missing_tool",),
        permissions=("read:catalog",),
    )

    with pytest.raises(InjectionError, match="missing_tool"):
        resolve_tool_surface(
            definition,
            ToolRegistry(),
            granted_permissions=("read:catalog",),
        )
