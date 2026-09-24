from __future__ import annotations

import pytest


def _connector() -> str:
    return "ok"


def test_register_and_get_round_trip() -> None:
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

    registry = ToolRegistry()
    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )

    registry.register(spec)

    assert registry.get("catalog_search") == spec


def test_get_unknown_tool_raises_not_found() -> None:
    from agents_system.harness.registry import ToolNotFoundError, ToolRegistry

    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="missing"):
        registry.get("missing")


def test_register_duplicate_name_raises() -> None:
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

    registry = ToolRegistry()
    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )

    registry.register(spec)

    with pytest.raises(ValueError, match="catalog_search"):
        registry.register(spec)


def test_toolspec_has_description_and_schema_defaults() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="x",
        required_permissions=(),
        connector=lambda i: i,
        tier=Tier.T0,
    )

    assert spec.description == ""
    assert spec.input_schema == {}


def test_to_langchain_tool_schema_shape() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=lambda i: i,
        tier=Tier.T1,
        description="Search catalog",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    schema = spec.to_langchain_tool_schema()

    assert schema["type"] == "function"
    func = schema["function"]
    assert func["name"] == "catalog_search"
    assert func["description"] == "Search catalog"
    assert func["parameters"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }


def test_toolspec_always_revalidate_defaults_false() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )

    assert spec.always_revalidate is False


def test_toolspec_accepts_always_revalidate_true() -> None:
    from agents_system.harness.registry import Tier, ToolSpec

    spec = ToolSpec(
        name="sensitive_read",
        required_permissions=("read:orders",),
        connector=_connector,
        tier=Tier.T1,
        always_revalidate=True,
    )

    assert spec.always_revalidate is True


def test_contains_and_names_reflect_registered_tools() -> None:
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

    registry = ToolRegistry()
    first = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )
    second = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_connector,
        tier=Tier.T2,
    )

    registry.register(first)
    registry.register(second)

    assert "catalog_search" in registry
    assert "missing" not in registry
    assert registry.names() == ("catalog_search", "order_writer")
