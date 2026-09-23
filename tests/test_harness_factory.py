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
# platform/roles/sales-agent/manifest.md). The generic deployment inherits them.
SALES_PERMISSIONS = [
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
]


def _fixture_roots() -> Any:
    """RootConfig pointing at the simple-role fixtures."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _client_a_roots() -> Any:
    """RootConfig for platform roles and generic client deployment fixtures."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=REPO_ROOT / "platform",
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _spec(name: str, perms: list[str]) -> Any:
    from agentsys.harness.registry import Tier, ToolSpec

    # Mirror the pre-tier write:/send: heuristic so fixtures keep their
    # original sensitivity classification (ADR-002 C.10).
    tier = Tier.T2 if any(p.startswith(("write:", "send:")) for p in perms) else Tier.T1
    return ToolSpec(
        name=name,
        required_permissions=tuple(perms),
        connector=lambda: None,
        tier=tier,
    )


def _sales_registry() -> Any:
    """Registry holding the RESOLVED sales-agent surface.

    Six tools, not five: `escalation_notifier` is inherited from
    `platform/roles/agent`. The injector raises `InjectionError: Unknown tool`
    for any declared tool the registry lacks, and it grades against the
    resolved chain, so a registry built from the leaf manifest alone no longer
    boots the role.
    """
    from agentsys.harness.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_spec("message_sender", ["send:message"]))
    reg.register(_spec("catalog_search", ["read:catalog"]))
    reg.register(_spec("order_writer", ["write:orders", "write:order_items"]))
    reg.register(_spec("session_state", []))
    reg.register(_spec("client_lookup", ["read:client_registry"]))
    reg.register(_spec("escalation_notifier", ["send:escalation"]))
    return reg


# ---------------------------------------------------------------------------
# Happy path — generic client deployment
# ---------------------------------------------------------------------------
def test_build_runtime_attaches_resolved_definition() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    assert runtime.definition.role_name == "sales-agent"
    assert runtime.definition.deployment == "client-a"


def test_build_runtime_grants_all_tools_when_permitted() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
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
        client="client-a",
        roots=_client_a_roots(),
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
        client="client-a",
        roots=_client_a_roots(),
    )

    skill_names = [s.name for s in runtime.skills]
    assert skill_names == [
        "request_structuring",
        "query_normalization",
        "confirmation_workflow",
    ]
    request_skill = next(s for s in runtime.skills if s.name == "request_structuring")
    assert "Structured request fields" in request_skill.content


def test_build_runtime_composes_prompt_from_role_body_and_skills() -> None:
    from agentsys.harness.factory import build_runtime

    runtime = build_runtime(
        "sales-agent",
        _sales_registry(),
        SALES_PERMISSIONS,
        client="client-a",
        roots=_client_a_roots(),
    )

    prompt = runtime.system_prompt
    assert "Generic Client A request assistant" in prompt
    # Each skill body is concatenated into the composed prompt.
    assert "Structured request fields" in prompt
    # Skills appear AFTER the role body.
    assert prompt.index("Generic Client A request assistant") < prompt.index(
        "Structured request fields"
    )


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
            client="client-a",
            roots=_client_a_roots(),
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
            client="client-a",
            roots=_client_a_roots(),
        )

    loaded = [e for e in logs if e["event"] == "factory.skill_loaded"]
    assert {e["skill"] for e in loaded} == {
        "request_structuring",
        "query_normalization",
        "confirmation_workflow",
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


def test_loading_skills_with_an_absent_deployments_root_raises_clearly(
    tmp_path: pathlib.Path,
) -> None:
    """An absent root must not be reported as missing skill files.

    `_load_skills` joins `deployments_root` into a path and then checks each
    skill file. With the root absent, every skill "does not exist", so the
    failure surfaced as `FactoryError: skill 'x' has no file at ...` —
    reading as a deployment authoring mistake when the real cause is a
    consumer that never passed a root. The guard now fires first and names
    what is actually wrong.
    """
    import pytest

    from agentsys.harness.factory import _load_skills
    from agentsys.harness.loader import AgentDefinition, DefinitionError, RootConfig

    definition = AgentDefinition(
        role_name="simple-role",
        version="1.0",
        deployment="client-a",
        system_prompt="body",
        tools=(),
        skills=("some_skill",),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    roots = RootConfig(
        platform_root=tmp_path / "platform",
        deployments_root=tmp_path / "absent",
    )

    with pytest.raises(DefinitionError) as excinfo:
        _load_skills(definition, "client-a", roots)

    assert "absent" in str(excinfo.value)
