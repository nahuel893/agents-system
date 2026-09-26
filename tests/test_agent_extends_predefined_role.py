"""Tests for `extends: str` targeting a predefined role or the generic agent
— additive inheritance, Option B (design.md D3, PR2-T3;
agent-definition-locator spec's "Additive inheritance from the generic agent
or any predefined role (Option B)").

Test-only per the task: the resolution path already exists from PR1a's chain
walk + `_fold_parent_into_child` (unchanged by this PR) and PR2-T1's
`_to_locator()` — this file proves the composed result, rather than adding
new production code.
"""

from __future__ import annotations

from platform_role_contract import EXPECTED_ROLE_TOOLS

from agents_system.harness.loader import RootConfig, resolve


def test_agent_extending_sales_agent_inherits_its_full_tool_surface() -> None:
    from agents_system.agent.spec import Agent

    # `sales-agent` declares `untrusted_input: true` (it handles customer
    # input directly), so the one added permission must be a real,
    # registered non-T3 permission — `_validate_untrusted_input_exec` looks
    # up every held permission by wire name and only T3 is forbidden here.
    agent = Agent(
        name="vip-sales",
        extends="platform/roles/sales-agent",
        tools=("vip_perk",),
        permissions=("read:catalog",),
    )

    definition = resolve(agent._to_locator(), roots=RootConfig())

    assert EXPECTED_ROLE_TOOLS["sales-agent"] <= set(definition.tools)
    assert "vip_perk" in definition.tools
    assert "read:catalog" in definition.permissions


def test_agent_extending_the_generic_agent_starts_from_the_minimal_floor() -> None:
    """No predefined-role ancestor: the resolved agent inherits only
    `agent`'s own floor (`session_state`/`escalation_notifier`,
    `read:session`/`send:escalation`, `supervised` autonomy) before its own
    fields fold on top — never a predefined role's extra tools."""
    from agents_system.agent.spec import Agent

    agent = Agent(name="triage-bot", extends="agent", tools=("catalog_search",))

    definition = resolve(agent._to_locator(), roots=RootConfig())

    assert set(definition.tools) == {
        "session_state",
        "escalation_notifier",
        "catalog_search",
    }
    assert set(definition.permissions) >= {"read:session", "send:escalation"}
    assert definition.autonomy == "supervised"
