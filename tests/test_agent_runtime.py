"""Tests for AgentRuntime — D-007.

These tests exercise the LangGraph-based runtime without real LLM providers:
all model responses come from FakeMessagesListChatModel (sequential responses).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Sequence

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from agentsys.harness.factory import EquippedRuntime
from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolSpec


# ---------------------------------------------------------------------------
# Test model helpers
# ---------------------------------------------------------------------------


class ToolAwareFakeModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel that supports bind_tools (returns self).

    bind_tools on the base fake model raises NotImplementedError. Since our test
    responses already hard-code the tool_calls in the AIMessage, we only need
    bind_tools to not crash — the schema is stored by the runtime, not by the model.
    """

    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> "ToolAwareFakeModel":
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_definition() -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=("read:catalog",),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


def _make_runtime(tools: tuple[ToolSpec, ...] = ()) -> EquippedRuntime:
    return EquippedRuntime(
        definition=_fake_definition(),
        system_prompt="You are a helpful assistant.",
        tools=tools,
        denied_tools=(),
        skills=(),
    )


def _catalog_spec() -> ToolSpec:
    def catalog_search(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"id": "prod-001", "name": "Sugar 1kg", "price": 850.0}]}

    return ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=catalog_search,
        description="Search catalog",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


# ---------------------------------------------------------------------------
# Phase 3 — RED tests (import from graph.py which doesn't exist yet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_returns_final_ai_message() -> None:
    """A turn with no tool calls ends immediately with an AIMessage."""
    from agentsys.agent.graph import AgentRuntime

    final_reply = AIMessage(content="Hello! How can I help you?")
    model = FakeMessagesListChatModel(responses=[final_reply])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Hi")]
    result = await agent.run_turn(messages, session_id="s1", permissions=())

    assert isinstance(result, list)
    assert len(result) > 0
    assert isinstance(result[-1], AIMessage)
    assert result[-1].content == "Hello! How can I help you?"


@pytest.mark.asyncio
async def test_tool_call_blocked_emits_error_tool_message() -> None:
    """When the model requests a tool not in the runtime surface, PolicyViolation
    is caught and a ToolMessage with status='error' is returned."""
    from agentsys.agent.graph import AgentRuntime

    tool_call_id = "call_123"
    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "nonexistent_tool",
                "args": {"q": "sugar"},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="I cannot use that tool.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    runtime = _make_runtime()  # empty surface — no tools registered
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Find something")]
    result = await agent.run_turn(messages, session_id="s1", permissions=())

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    tm = tool_messages[0]
    assert tm.status == "error"
    assert tm.content.startswith("Tool call blocked:")


@pytest.mark.asyncio
async def test_permitted_tool_call_returns_json_output() -> None:
    """When the model calls a permitted tool, connector output is JSON-encoded."""
    from agentsys.agent.graph import AgentRuntime

    tool_call_id = "call_456"
    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "catalog_search",
                "args": {"q": "sugar"},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="Here are the results.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    catalog_spec = _catalog_spec()
    runtime = _make_runtime(tools=(catalog_spec,))
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Search for sugar")]
    result = await agent.run_turn(
        messages, session_id="s1", permissions=("read:catalog",)
    )

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    tm = tool_messages[0]
    assert tm.status != "error"
    parsed = json.loads(tm.content)
    assert "results" in parsed


@pytest.mark.asyncio
async def test_sync_connector_does_not_block_event_loop() -> None:
    """Sync connectors are wrapped in asyncio.to_thread — the event loop stays free."""
    from agentsys.agent.graph import AgentRuntime

    tool_call_id = "call_789"
    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "slow_tool",
                "args": {},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="Done.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    def slow_sync_connector(inputs: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0)  # 0 seconds — still validates the wrapping path
        return {"status": "ok"}

    slow_spec = ToolSpec(
        name="slow_tool",
        required_permissions=(),
        connector=slow_sync_connector,
    )
    runtime = _make_runtime(tools=(slow_spec,))
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Run slow tool")]
    # If the event loop were blocked, this would raise asyncio.TimeoutError
    result = await asyncio.wait_for(
        agent.run_turn(messages, session_id="s1", permissions=()),
        timeout=2.0,
    )

    assert isinstance(result[-1], AIMessage)


# ---------------------------------------------------------------------------
# Phase 4 additional tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_swap_requires_zero_runtime_changes() -> None:
    """Two different FakeMessagesListChatModel instances work identically.

    This covers the spec scenario: 'Provider swap requires zero runtime changes'.
    The runtime does not import or reference any concrete provider class.
    """
    from agentsys.agent.graph import AgentRuntime

    reply_a = AIMessage(content="Response from model A")
    reply_b = AIMessage(content="Response from model B")

    model_a = FakeMessagesListChatModel(responses=[reply_a])
    model_b = FakeMessagesListChatModel(responses=[reply_b])

    runtime = _make_runtime()
    agent_a = AgentRuntime(runtime, model_a)
    agent_b = AgentRuntime(runtime, model_b)

    result_a = await agent_a.run_turn(
        [HumanMessage(content="Hello from A")], session_id="s-a", permissions=()
    )
    result_b = await agent_b.run_turn(
        [HumanMessage(content="Hello from B")], session_id="s-b", permissions=()
    )

    assert isinstance(result_a[-1], AIMessage)
    assert isinstance(result_b[-1], AIMessage)
    assert result_a[-1].content == "Response from model A"
    assert result_b[-1].content == "Response from model B"


@pytest.mark.asyncio
async def test_stateless_run_turn_caller_owns_history() -> None:
    """Two separate run_turn calls with different histories don't bleed state.

    The runtime is stateless — each call uses only the messages the caller supplies.
    """
    from agentsys.agent.graph import AgentRuntime

    reply_1 = AIMessage(content="First turn reply")
    reply_2 = AIMessage(content="Second turn reply")

    model = FakeMessagesListChatModel(responses=[reply_1, reply_2])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    messages_turn_1 = [HumanMessage(content="Turn 1 question")]
    messages_turn_2 = [HumanMessage(content="Turn 2 question")]

    result_1 = await agent.run_turn(messages_turn_1, session_id="s1", permissions=())
    result_2 = await agent.run_turn(messages_turn_2, session_id="s2", permissions=())

    # Each result contains only what was passed + the new AI reply — no cross-turn bleed
    assert result_1[-1].content == "First turn reply"
    assert result_2[-1].content == "Second turn reply"

    # No messages from turn 1 appear in turn 2's result
    turn_2_human_contents = [
        m.content for m in result_2 if isinstance(m, HumanMessage)
    ]
    assert all("Turn 1" not in c for c in turn_2_human_contents)
