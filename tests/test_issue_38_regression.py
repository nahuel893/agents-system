"""Regression tests for issue #38 — Layer-2 revalidation checks the role's
full permissions, not the effective grant.

Before this fix, Layer-2 (`interceptor.intercept` / `AgentRuntime.run_turn`'s
default) fell back to the role's FULL declared permission set
(`definition.permissions`) whenever no explicit `permissions` override was
passed to a turn — wider than what the runtime was actually granted at boot
(`EquippedRuntime.deploy_grant_ceiling`). Layer-1 (the injector) already
bounds tool EQUIPMENT to the deploy grant, so a tool dropped there is
probably not callable — but the second layer of defense was not checking
what it claims to check: the effective grant.

These tests prove Layer-2 now bounds itself to `deploy_grant_ceiling`
instead, both when `current_permissions` is passed explicitly to
`intercept()` (spec: "Layer-2 revalidates against the persisted deploy grant
ceiling (issue #38)") and when `AgentRuntime.run_turn` falls back to its own
default (the actual production path — neither the webhook worker nor the
OpenAI adapter passes an explicit `permissions` override).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.interceptor import PolicyViolation, intercept
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.permissions import permission_registry


def _order_writer_connector(_inputs: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok"}


def _order_writer_spec() -> ToolSpec:
    return ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_order_writer_connector,
        tier=Tier.T2,
    )


def _definition(permissions: tuple[str, ...]) -> AgentDefinition:
    """A role that DECLARES `permissions` — distinct from what a deployment
    actually GRANTS (deploy_grant_ceiling, set separately below)."""
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=("order_writer",),
        skills=(),
        context={},
        permissions=permissions,
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
        untrusted_input=False,
    )


def _runtime(
    *, role_permissions: tuple[str, ...], deploy_grant_ceiling: frozenset[Any]
) -> EquippedRuntime:
    return EquippedRuntime(
        definition=_definition(role_permissions),
        system_prompt="",
        tools=(_order_writer_spec(),),
        denied_tools=(),
        skills=(),
        deploy_grant_ceiling=deploy_grant_ceiling,
    )


# ---------------------------------------------------------------------------
# Interceptor-level: the spec's exact scenarios
# ---------------------------------------------------------------------------


async def test_narrower_deploy_grant_denies_at_layer2_despite_role_permission() -> None:
    """Scenario: Narrower deploy grant denies at Layer-2 even though the
    role permits (issue #38 regression). A role declares `write:orders`
    among its permissions, but the deploy-time grant equips only
    `Read`-family permissions — narrower than the role. Layer-2 MUST deny."""
    read_catalog_cls = permission_registry.resolve("read:catalog")

    runtime = _runtime(
        role_permissions=("write:orders",),  # the role permits it...
        deploy_grant_ceiling=frozenset(
            {read_catalog_cls}
        ),  # ...the deploy grant doesn't
    )

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "order_writer",
            {"items": []},
            runtime,
            # Even a caller claiming write:orders in current_permissions
            # cannot widen past the persisted deploy grant ceiling.
            current_permissions=["write:orders"],
        )

    assert exc_info.value.reason == "permission_revoked"


async def test_grant_covered_tool_passes_layer2() -> None:
    """Scenario: Grant-covered tool passes Layer-2. The deploy grant
    ceiling includes write:orders and the current principal's permissions
    include it too — Layer-2 grants the call."""
    write_orders_cls = permission_registry.resolve("write:orders")

    runtime = _runtime(
        role_permissions=("write:orders",),
        deploy_grant_ceiling=frozenset({write_orders_cls}),
    )

    result = await intercept(
        "order_writer",
        {"items": []},
        runtime,
        current_permissions=["write:orders"],
    )

    assert result.revalidated is True
    assert result.output == {"status": "ok"}


# ---------------------------------------------------------------------------
# Full-stack: AgentRuntime.run_turn's DEFAULT (no explicit `permissions`) —
# the actual production path (webhook worker, OpenAI adapter never pass one)
# ---------------------------------------------------------------------------


class _ToolAwareFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolAwareFakeModel:
        return self


async def test_run_turn_default_permissions_bounded_by_deploy_grant_ceiling() -> None:
    """AgentRuntime.run_turn's default (no explicit `permissions` passed)
    must not widen back out to the role's full permission set either — this
    is the actual issue #38 production path."""
    from agents_system.agent.graph import AgentRuntime

    first_response = AIMessage(
        content="",
        tool_calls=[
            {"id": "call_1", "name": "order_writer", "args": {}, "type": "tool_call"}
        ],
    )
    model = _ToolAwareFakeModel(responses=[first_response])

    read_catalog_cls = permission_registry.resolve("read:catalog")
    runtime = _runtime(
        role_permissions=("write:orders",),  # the role permits it...
        deploy_grant_ceiling=frozenset(
            {read_catalog_cls}
        ),  # ...the deploy grant doesn't
    )
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn(
        [HumanMessage(content="place an order")], session_id="s1"
    )

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"


async def test_run_turn_default_permissions_allow_grant_covered_tool() -> None:
    """Positive counterpart: when the deploy grant DOES cover the required
    permission, run_turn's default lets the sensitive tool through."""
    from agents_system.agent.graph import AgentRuntime

    first_response = AIMessage(
        content="",
        tool_calls=[
            {"id": "call_1", "name": "order_writer", "args": {}, "type": "tool_call"}
        ],
    )
    final_response = AIMessage(content="Order placed.")
    model = _ToolAwareFakeModel(responses=[first_response, final_response])

    write_orders_cls = permission_registry.resolve("write:orders")
    runtime = _runtime(
        role_permissions=("write:orders",),
        deploy_grant_ceiling=frozenset({write_orders_cls}),
    )
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn(
        [HumanMessage(content="place an order")], session_id="s1"
    )

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status != "error"
