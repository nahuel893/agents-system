"""Tests for the agent factory — the assembler (D-004).

The factory is the glue layer: it takes a resolved ``AgentDefinition`` (loader),
resolves the granted tool surface (injector), loads the deployment's skill files
from disk, composes the final system prompt, and returns a frozen
``EquippedRuntime``.

It is the keystone that makes everything below it usable by the Agent Runtime
(LangGraph / bind_tools), which is a LATER slice — the factory does NOT call an
LLM or bind tools to a model.

Strict TDD: these tests are written before factory.py exists.
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

# The six permissions the platform sales-agent declares (see
# platform/roles/sales-agent/manifest.md). The BADIE deployment inherits them.
SALES_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
]


def _fixture_roots() -> Any:
    """RootConfig pointing at the test fixtures (isolated from the real repo)."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _spec(name: str, perms: list[str]) -> Any:
    from agentsys.harness.registry import ToolSpec

    return ToolSpec(name=name, required_permissions=tuple(perms), connector=lambda: None)


def _sales_registry() -> Any:
    """Registry holding the five BADIE sales-agent tools with required perms."""
    from agentsys.harness.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec("message_sender", ["send:message"]))
    reg.register(_spec("catalog_search", ["read:catalog"]))
    reg.register(_spec("order_writer", ["write:orders", "write:order_items"]))
    reg.register(_spec("session_state", []))
    reg.register(_spec("client_lookup", ["read:client_registry"]))
    return reg


# ---------------------------------------------------------------------------
# Happy path — real BADIE deployment (the MVP target)
# ---------------------------------------------------------------------------
def test_build_runtime_attaches_resolved_definition() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="badie",
    )

    assert runtime.definition.role_name == "sales-agent"
    assert runtime.definition.deployment == "badie"


def test_build_runtime_grants_all_tools_when_permitted() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="badie",
    )

    granted_names = {t.name for t in runtime.tools}
    assert granted_names == {
        "message_sender",
        "catalog_search",
        "order_writer",
        "session_state",
        "client_lookup",
    }
    assert runtime.denied_tools == ()


def test_build_runtime_denies_tools_missing_permissions() -> None:
    from agentsys.harness.factory import build_runtime

    # Only read:catalog granted → catalog_search (needs read:catalog) and
    # session_state (needs nothing) pass; the rest are denied.
    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        ["read:catalog"],
        client="badie",
    )

    granted_names = {t.name for t in runtime.tools}
    assert granted_names == {"catalog_search", "session_state"}

    denied_names = {name for name, _reason in runtime.denied_tools}
    assert denied_names == {"message_sender", "order_writer", "client_lookup"}


def test_build_runtime_loads_declared_skills_in_order() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="badie",
    )

    skill_names = [s.name for s in runtime.skills]
    assert skill_names == ["order_extraction", "colloquial_matching", "confirm_flow"]
    # Skill content is the real file body, not a placeholder.
    order_skill = next(s for s in runtime.skills if s.name == "order_extraction")
    assert "Extraction rules" in order_skill.content


def test_build_runtime_composes_prompt_from_role_body_and_skills() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="badie",
    )

    prompt = runtime.system_prompt
    # Role body content (deployments/badie/sales-agent/role.md).
    assert "punto de venta" in prompt
    # Each skill body is concatenated into the composed prompt.
    assert "Extraction rules" in prompt
    # Skills appear AFTER the role body.
    assert prompt.index("punto de venta") < prompt.index("Extraction rules")


# ---------------------------------------------------------------------------
# Generic role (no client) — no skills, prompt is the role body alone
# ---------------------------------------------------------------------------
def test_build_runtime_generic_role_has_no_skills() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime("sales-agent", _sales_registry(), SALES_PERMISSIONS)

    assert runtime.skills == ()
    assert runtime.definition.deployment is None
    # With no skills the composed prompt is exactly the role body (stripped).
    assert runtime.system_prompt == runtime.definition.system_prompt.strip()


# ---------------------------------------------------------------------------
# Failure mode — a declared skill with no file on disk fails loud
# ---------------------------------------------------------------------------
def test_build_runtime_missing_skill_file_raises() -> None:
    from agentsys.harness.factory import FactoryError, build_runtime
    from agentsys.harness.registry import ToolRegistry

    # client-a/simple-role declares skills [skill_one, skill_two] but the
    # fixture has no skills/ directory → the factory must fail loud.
    reg = ToolRegistry()
    reg.register(_spec("tool_alpha", ["read:alpha"]))
    reg.register(_spec("tool_beta", ["read:beta"]))

    with pytest.raises(FactoryError):
        build_runtime(
            "simple-role",
            reg,
            ["read:alpha", "read:beta", "write:gamma"],
            client="client-a",
            roots=_fixture_roots(),
        )


# ---------------------------------------------------------------------------
# Defensive guard — skills declared but no client to load them from
# ---------------------------------------------------------------------------
def test_load_skills_without_client_raises() -> None:
    from agentsys.harness.factory import FactoryError, _load_skills
    from agentsys.harness.loader import AgentDefinition

    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=(),
        skills=("ghost_skill",),  # declared, but client is None below
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    with pytest.raises(FactoryError):
        _load_skills(definition, None, _fixture_roots())


# ---------------------------------------------------------------------------
# Auditability — building a runtime emits a structured event
# ---------------------------------------------------------------------------
def test_build_runtime_logs_built_event() -> None:
    from agentsys.harness.factory import build_runtime

    with structlog.testing.capture_logs() as logs:
        build_runtime(
            "sales-agent",
            _sales_registry(),
            SALES_PERMISSIONS,
            client="badie",
        )

    events = [e["event"] for e in logs]
    assert "factory.runtime_built" in events


def test_build_runtime_logs_each_skill_loaded() -> None:
    from agentsys.harness.factory import build_runtime

    with structlog.testing.capture_logs() as logs:
        build_runtime(
            "sales-agent",
            _sales_registry(),
            SALES_PERMISSIONS,
            client="badie",
        )

    loaded = [e for e in logs if e["event"] == "factory.skill_loaded"]
    assert {e["skill"] for e in loaded} == {
        "order_extraction",
        "colloquial_matching",
        "confirm_flow",
    }


def test_build_runtime_logs_skill_missing_before_raising() -> None:
    from agentsys.harness.factory import FactoryError, build_runtime
    from agentsys.harness.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec("tool_alpha", ["read:alpha"]))
    reg.register(_spec("tool_beta", ["read:beta"]))

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(FactoryError):
            build_runtime(
                "simple-role",
                reg,
                ["read:alpha", "read:beta", "write:gamma"],
                client="client-a",
                roots=_fixture_roots(),
            )

    events = [e["event"] for e in logs]
    assert "factory.skill_missing" in events
