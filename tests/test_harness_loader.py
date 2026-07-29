"""Tests for the agent definition loader (src/agentsys/harness/loader.py).

Tests follow strict TDD: written BEFORE the implementation, intentionally fail
until the module exists and passes all invariants.

Fixture roots
-------------
- GENERIC_ROOTS  → points to tests/fixtures/agents/generic-role/
- OVERRIDE_ROOTS → points to tests/fixtures/agents/overrides/
- REAL_ROOTS     → points to the actual repo platform/ and deployments/ folders

The `roots` parameter accepted by all loader functions is a RootConfig object
with two fields:
    platform_root  — path that contains a `roles/` subdirectory
    deployments_root — path that contains `{client}/{role_type}/` subdirectories
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = pathlib.Path(__file__).parent.parent
FIXTURE_BASE = REPO_ROOT / "tests" / "fixtures" / "agents"

GENERIC_ROOTS_DIR = FIXTURE_BASE / "generic-role"
OVERRIDE_ROOTS_DIR = FIXTURE_BASE / "overrides"


def _generic_roots() -> Any:
    """RootConfig pointing at the fixture generic-role tree."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _override_roots() -> Any:
    """RootConfig pointing at the fixture override tree."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=GENERIC_ROOTS_DIR,
        deployments_root=OVERRIDE_ROOTS_DIR / "deployments",
    )


def _real_roots() -> Any:
    """RootConfig pointing at the actual repo files."""
    from agentsys.harness.loader import RootConfig

    return RootConfig(
        platform_root=REPO_ROOT / "platform",
        deployments_root=REPO_ROOT / "deployments",
    )


# ---------------------------------------------------------------------------
# Test 1 — Loading a generic role produces a fully-populated AgentDefinition
# ---------------------------------------------------------------------------
def test_load_generic_produces_agent_definition() -> None:
    from agentsys.harness.loader import AgentDefinition, resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert isinstance(definition, AgentDefinition)
    assert definition.role_name == "simple-role"
    assert definition.version == "1.0"
    assert definition.deployment is None


def test_load_generic_tools_parsed() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert set(definition.tools) == {"tool_alpha", "tool_beta", "tool_gamma"}


def test_load_generic_permissions_parsed() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert set(definition.permissions) == {"read:alpha", "read:beta", "write:gamma"}


def test_load_generic_autonomy_parsed() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert definition.autonomy == "supervised"


def test_load_generic_delegation_policy_parsed() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert definition.delegation_policy["allowed"] is False
    assert definition.delegation_policy["max_depth"] == 0


# ---------------------------------------------------------------------------
# Test 2 — role.md prose body captured verbatim as system_prompt
# ---------------------------------------------------------------------------
def test_role_md_body_captured_as_system_prompt() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    # The body starts after the closing --- of the frontmatter
    assert "# Role: simple-role" in definition.system_prompt
    assert "## purpose" in definition.system_prompt
    # The frontmatter itself must NOT appear in system_prompt
    assert "name: simple-role" not in definition.system_prompt


# ---------------------------------------------------------------------------
# Test 3 — resolve with no client returns the generic definition unchanged
# ---------------------------------------------------------------------------
def test_resolve_no_client_returns_generic() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    assert definition.deployment is None
    assert definition.skills == ()


# ---------------------------------------------------------------------------
# Test 4 — resolve with a client that has an override merges correctly
# ---------------------------------------------------------------------------
def test_resolve_with_client_merges_override() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", client="client-a", roots=_override_roots())

    # deployment field populated
    assert definition.deployment == "client-a"
    # tools are the subset declared in the override manifest
    assert set(definition.tools) == {"tool_alpha", "tool_beta"}
    # skills come from the override
    assert set(definition.skills) == {"skill_one", "skill_two"}
    # escalation_rules: base conditions + client-specific addition
    conditions = definition.escalation_rules["conditions"]
    assert "required_tool_missing" in conditions
    assert "client_specific_condition" in conditions


# ---------------------------------------------------------------------------
# Test 5 — permissions: inherit keyword resolves to the parent's full set
# ---------------------------------------------------------------------------
def test_permissions_inherit_keyword_resolves_to_parent() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", client="client-a", roots=_override_roots())

    # Override declares `permissions: inherit` — must equal parent set exactly
    assert set(definition.permissions) == {"read:alpha", "read:beta", "write:gamma"}


# ---------------------------------------------------------------------------
# Test 6 — {inherit: true, add: [...]} appends to parent list (dedup, order)
# ---------------------------------------------------------------------------
def test_escalation_rules_inherit_add_appends() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", client="client-a", roots=_override_roots())

    conditions = definition.escalation_rules["conditions"]
    # Both the parent conditions and the new one must be present
    assert "required_tool_missing" in conditions
    assert "confidence_below_threshold" in conditions
    assert "client_specific_condition" in conditions
    # No duplicates
    assert len(conditions) == len(set(conditions))


# ---------------------------------------------------------------------------
# Test 7 — Invariant: override tool NOT in parent raises DefinitionError
# ---------------------------------------------------------------------------
def test_override_tool_not_in_parent_raises_definition_error() -> None:
    from agentsys.harness.loader import DefinitionError, resolve

    with pytest.raises(DefinitionError, match="tools"):
        resolve("simple-role", client="bad-tools", roots=_override_roots())


# ---------------------------------------------------------------------------
# Test 8 — Invariant: override autonomy above parent ceiling raises DefinitionError
# ---------------------------------------------------------------------------
def test_override_autonomy_elevation_raises_definition_error() -> None:
    from agentsys.harness.loader import DefinitionError, resolve

    with pytest.raises(DefinitionError, match="autonomy"):
        resolve("simple-role", client="bad-autonomy", roots=_override_roots())


# ---------------------------------------------------------------------------
# Test 9 — Override folder absent → resolve returns generic without error
# ---------------------------------------------------------------------------
def test_resolve_absent_override_folder_returns_generic() -> None:
    from agentsys.harness.loader import resolve

    # "nonexistent-client" has no folder under the override deployments root
    definition = resolve(
        "simple-role",
        client="nonexistent-client",
        roots=_override_roots(),
    )

    assert definition.deployment is None
    assert definition.skills == ()


# ---------------------------------------------------------------------------
# Test 10 — Real BADIE sales-agent merge (happy-path integration test)
# ---------------------------------------------------------------------------
def test_real_badie_sales_agent_merge() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("sales-agent", client="badie", roots=_real_roots())

    assert definition.role_name == "sales-agent"
    assert definition.deployment == "badie"

    # Skills must come from the BADIE override
    assert set(definition.skills) == {
        "order_extraction",
        "colloquial_matching",
        "confirm_flow",
    }

    # Permissions: override says `inherit` — must match parent exactly
    expected_permissions = {
        "read:catalog",
        "read:client_registry",
        "write:orders",
        "write:order_items",
        "read:price_lists",
        "send:message",
    }
    assert set(definition.permissions) == expected_permissions

    # Escalation: parent conditions + BADIE addition
    conditions = definition.escalation_rules["conditions"]
    assert "customer_not_registered" in conditions
    assert "three_failed_clarification_attempts" in conditions

    # Autonomy must not exceed the platform ceiling of supervised
    assert definition.autonomy == "supervised"

    # system_prompt must contain the role.md body
    assert "sales-agent" in definition.system_prompt


# ---------------------------------------------------------------------------
# Test 11 — system_prompt is the role.md body from the deployment override
# (when a deployment role.md exists, it is the effective system prompt)
# ---------------------------------------------------------------------------
def test_real_badie_system_prompt_is_override_role_body() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("sales-agent", client="badie", roots=_real_roots())

    # The BADIE role.md body mentions BADIE-specific vocabulary/purpose
    assert "BADIE" in definition.system_prompt or "badie" in definition.system_prompt.lower()


# ---------------------------------------------------------------------------
# Test 13 — execution_limits: stricter limit merges successfully
# ---------------------------------------------------------------------------
def test_execution_limits_stricter_override_merges() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", client="strict-limits", roots=_override_roots())

    # max_tool_calls: 10 is stricter than platform default 20
    assert definition.execution_limits is not None
    assert definition.execution_limits["max_tool_calls"] == 10


# ---------------------------------------------------------------------------
# Test 14 — execution_limits: looser limit raises DefinitionError
# ---------------------------------------------------------------------------
def test_execution_limits_looser_override_raises_definition_error() -> None:
    from agentsys.harness.loader import DefinitionError, resolve

    with pytest.raises(DefinitionError, match="max_tool_calls"):
        resolve("simple-role", client="loose-limits", roots=_override_roots())


# ---------------------------------------------------------------------------
# Test 15 — execution_limits: inherit still works (regression guard)
# ---------------------------------------------------------------------------
def test_execution_limits_inherit_still_works() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", client="client-a", roots=_override_roots())

    # client-a uses execution_limits: inherit
    # Since parent has execution_limits: null, resolved should be None
    assert definition.execution_limits is None


# ---------------------------------------------------------------------------
# Test 12 — AgentDefinition is frozen (immutable after construction)
# ---------------------------------------------------------------------------
def test_agent_definition_is_frozen() -> None:
    from agentsys.harness.loader import resolve

    definition = resolve("simple-role", roots=_generic_roots())

    with pytest.raises((AttributeError, TypeError)):
        definition.role_name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# D-014 S2 — PLATFORM_DEFAULT_LIMITS public alias (design AD-3)
# ---------------------------------------------------------------------------
def test_platform_default_limits_public_alias() -> None:
    """`agent/graph.py` resolves effective execution limits against this public
    constant — it must be importable outside the loader module."""
    from agentsys.harness.loader import PLATFORM_DEFAULT_LIMITS

    assert PLATFORM_DEFAULT_LIMITS["max_tool_calls"] == 20
    assert PLATFORM_DEFAULT_LIMITS["total_execution_timeout_s"] == 60
    assert PLATFORM_DEFAULT_LIMITS["tool_call_timeout_s"] == 10


# ---------------------------------------------------------------------------
# D-024 — default platform_root resolution (packaged wheel vs. dev checkout)
#
# Before D-024, `_REPO_ROOT` was a single hardcoded four-hop-up path. In an
# installed wheel that lands above site-packages, where no `platform/`
# exists, so `resolve()` raised a confusing per-file DefinitionError instead
# of naming the actual problem. These tests drive both resolution branches
# via monkeypatch + tmp_path — they must NOT assert on this machine's real
# repo layout (that is covered separately by test_real_badie_sales_agent_merge
# using REAL_ROOTS).
# ---------------------------------------------------------------------------
def test_default_platform_root_prefers_packaged_location(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentsys.harness.loader as loader_module

    packaged = tmp_path / "packaged" / "platform"
    packaged.mkdir(parents=True)
    checkout = tmp_path / "checkout" / "platform"  # deliberately absent

    monkeypatch.setattr(loader_module, "_PACKAGED_PLATFORM_ROOT", packaged)
    monkeypatch.setattr(loader_module, "_CHECKOUT_PLATFORM_ROOT", checkout)

    resolved = loader_module._default_platform_root()

    assert resolved == packaged


def test_default_platform_root_falls_back_to_checkout_location(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentsys.harness.loader as loader_module

    packaged = tmp_path / "packaged" / "platform"  # deliberately absent
    checkout = tmp_path / "checkout" / "platform"
    checkout.mkdir(parents=True)

    monkeypatch.setattr(loader_module, "_PACKAGED_PLATFORM_ROOT", packaged)
    monkeypatch.setattr(loader_module, "_CHECKOUT_PLATFORM_ROOT", checkout)

    resolved = loader_module._default_platform_root()

    assert resolved == checkout


def test_default_platform_root_both_missing_names_both_paths(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentsys.harness.loader as loader_module
    from agentsys.harness.loader import DefinitionError

    packaged = tmp_path / "packaged" / "platform"
    checkout = tmp_path / "checkout" / "platform"

    monkeypatch.setattr(loader_module, "_PACKAGED_PLATFORM_ROOT", packaged)
    monkeypatch.setattr(loader_module, "_CHECKOUT_PLATFORM_ROOT", checkout)

    with pytest.raises(DefinitionError) as exc_info:
        loader_module._default_platform_root()

    message = str(exc_info.value)
    assert str(packaged) in message
    assert str(checkout) in message


def test_root_config_default_uses_packaged_then_checkout_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RootConfig() with no explicit platform_root must go through the same
    resolution — not a hardcoded single default."""
    import agentsys.harness.loader as loader_module

    packaged = tmp_path / "packaged" / "platform"
    packaged.mkdir(parents=True)
    checkout = tmp_path / "checkout" / "platform"  # deliberately absent

    monkeypatch.setattr(loader_module, "_PACKAGED_PLATFORM_ROOT", packaged)
    monkeypatch.setattr(loader_module, "_CHECKOUT_PLATFORM_ROOT", checkout)

    roots = loader_module.RootConfig()

    assert roots.platform_root == packaged


def test_root_config_deployments_root_default_untouched_by_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deployments_root` keeps its dev-checkout default regardless of the
    platform_root packaged/checkout resolution — a consumer's deployments are
    never shipped inside the package."""
    import agentsys.harness.loader as loader_module

    packaged = tmp_path / "packaged" / "platform"
    packaged.mkdir(parents=True)

    monkeypatch.setattr(loader_module, "_PACKAGED_PLATFORM_ROOT", packaged)

    roots = loader_module.RootConfig()

    assert roots.deployments_root == loader_module._DEFAULT_DEPLOYMENTS_ROOT
