"""Tests for harness structured logging.

The harness is the security core: its enforcement decisions (which tools are
granted/denied, which invariants are violated) MUST be auditable. These tests
assert that those decisions emit structured log events.

Strict TDD: written before the logging instrumentation exists.
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest
import structlog

REPO_ROOT = pathlib.Path(__file__).parent.parent
FIXTURE_BASE = REPO_ROOT / "tests" / "fixtures" / "agents"
GENERIC_ROOTS_DIR = FIXTURE_BASE / "generic-role"
OVERRIDE_ROOTS_DIR = FIXTURE_BASE / "overrides"


def _override_roots() -> Any:
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _make_definition(*, tools: list[str], permissions: list[str]) -> Any:
    from agentsys.harness.loader import AgentDefinition

    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment="badie",
        system_prompt="",
        tools=tuple(tools),
        skills=(),
        context={},
        permissions=tuple(permissions),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


def _spec(name: str, perms: list[str]) -> Any:
    from agentsys.harness.registry import ToolSpec

    return ToolSpec(name=name, required_permissions=tuple(perms), connector=lambda: None)


def _registry_with(*specs: Any) -> Any:
    from agentsys.harness.registry import ToolRegistry

    reg = ToolRegistry()
    for s in specs:
        reg.register(s)
    return reg


# ---------------------------------------------------------------------------
# Injector — granted tool emits an audit event
# ---------------------------------------------------------------------------
def test_injector_logs_granted_tool() -> None:
    from agentsys.harness.injector import resolve_tool_surface

    definition = _make_definition(tools=["catalog_search"], permissions=["read:catalog"])
    registry = _registry_with(_spec("catalog_search", ["read:catalog"]))

    with structlog.testing.capture_logs() as logs:
        resolve_tool_surface(definition, registry, ["read:catalog"])

    granted = [e for e in logs if e["event"] == "injector.tool_granted"]
    assert granted, "expected an injector.tool_granted event"
    assert granted[0]["tool"] == "catalog_search"
    assert granted[0]["role"] == "sales-agent"


# ---------------------------------------------------------------------------
# Injector — denied tool emits an audit event with a reason
# ---------------------------------------------------------------------------
def test_injector_logs_denied_tool() -> None:
    from agentsys.harness.injector import resolve_tool_surface

    definition = _make_definition(tools=["order_writer"], permissions=["write:orders"])
    registry = _registry_with(_spec("order_writer", ["write:orders"]))

    with structlog.testing.capture_logs() as logs:
        # user identity was NOT granted write:orders -> effective is empty
        resolve_tool_surface(definition, registry, [])

    denied = [e for e in logs if e["event"] == "injector.tool_denied"]
    assert denied, "expected an injector.tool_denied event"
    assert denied[0]["tool"] == "order_writer"
    assert "reason" in denied[0]


# ---------------------------------------------------------------------------
# Injector — unknown tool is logged before raising
# ---------------------------------------------------------------------------
def test_injector_logs_unknown_tool() -> None:
    from agentsys.harness.injector import InjectionError, resolve_tool_surface

    definition = _make_definition(tools=["ghost_tool"], permissions=[])
    registry = _registry_with()  # empty registry

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(InjectionError):
            resolve_tool_surface(definition, registry, [])

    events = [e["event"] for e in logs]
    assert "injector.unknown_tool" in events


# ---------------------------------------------------------------------------
# Loader — an invariant violation is logged before raising
# ---------------------------------------------------------------------------
def test_loader_logs_invariant_violation() -> None:
    from agentsys.harness.loader import DefinitionError, resolve

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(DefinitionError):
            resolve("simple-role", client="bad-autonomy", roots=_override_roots())

    events = [e["event"] for e in logs]
    assert "loader.invariant_violation" in events
