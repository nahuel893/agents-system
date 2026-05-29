from __future__ import annotations

import pytest


def _connector() -> str:
    return "ok"


def test_register_and_get_round_trip() -> None:
    from badie.harness.registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
    )

    registry.register(spec)

    assert registry.get("catalog_search") == spec


def test_get_unknown_tool_raises_not_found() -> None:
    from badie.harness.registry import ToolNotFoundError, ToolRegistry

    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="missing"):
        registry.get("missing")


def test_register_duplicate_name_raises() -> None:
    from badie.harness.registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
    )

    registry.register(spec)

    with pytest.raises(ValueError, match="catalog_search"):
        registry.register(spec)


def test_contains_and_names_reflect_registered_tools() -> None:
    from badie.harness.registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    first = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
    )
    second = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_connector,
    )

    registry.register(first)
    registry.register(second)

    assert "catalog_search" in registry
    assert "missing" not in registry
    assert registry.names() == ("catalog_search", "order_writer")
