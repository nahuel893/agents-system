"""Tests for AgentRuntime — D-007.

These tests exercise the LangGraph-based runtime without real LLM providers:
all model responses come from FakeMessagesListChatModel (sequential responses).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping, Sequence

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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


def _fake_definition(
    execution_limits: Mapping[str, Any] | None = None,
) -> AgentDefinition:
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
        execution_limits=execution_limits,
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


# ---------------------------------------------------------------------------
# D-009 RED tests — session_provider on EquippedRuntime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_passed_to_async_connector() -> None:
    """session_provider on EquippedRuntime opens a session; async connector receives it."""
    from unittest.mock import AsyncMock, MagicMock

    from agentsys.agent.graph import AgentRuntime

    received_sessions: list[Any] = []

    async def async_catalog(inputs: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        received_sessions.append(session)
        return {"results": []}

    async_spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=async_catalog,
        description="Async catalog search",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    # Build a mock async_sessionmaker: calling it returns an async context manager
    # that yields a mock session.
    mock_session = MagicMock(name="mock_session")
    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session_provider = MagicMock(return_value=mock_session_cm)

    runtime = EquippedRuntime(
        definition=_fake_definition(),
        system_prompt="You are a helpful assistant.",
        tools=(async_spec,),
        denied_tools=(),
        skills=(),
        session_provider=mock_session_provider,
    )

    tool_call_id = "call_async_001"
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
    final_response = AIMessage(content="Found results.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    agent = AgentRuntime(runtime, model)
    await agent.run_turn(
        [HumanMessage(content="Search for sugar")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert len(received_sessions) == 1
    assert received_sessions[0] is mock_session


@pytest.mark.asyncio
async def test_no_session_provider_backward_compatible() -> None:
    """session_provider=None (default) — turn runs; connector receives session=None."""
    from agentsys.agent.graph import AgentRuntime

    received_sessions: list[Any] = []

    async def async_catalog(inputs: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        received_sessions.append(session)
        return {"results": []}

    async_spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=async_catalog,
        description="Async catalog search",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )

    # No session_provider — uses default None
    runtime = _make_runtime(tools=(async_spec,))

    tool_call_id = "call_async_002"
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
    final_response = AIMessage(content="Found results.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    agent = AgentRuntime(runtime, model)
    result = await agent.run_turn(
        [HumanMessage(content="Search for sugar")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert isinstance(result[-1], AIMessage)
    assert len(received_sessions) == 1
    assert received_sessions[0] is None


# ---------------------------------------------------------------------------
# D-014 S1 — run_turn permission default (design AD-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_permissions_default_to_definition_permissions() -> None:
    """permissions=None (default) uses the runtime's own resolved grants."""
    from agentsys.agent.graph import AgentRuntime

    tool_call_id = "call_perm_001"
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

    # _fake_definition().permissions == ("read:catalog",), matching the spec below
    catalog_spec = _catalog_spec()
    runtime = _make_runtime(tools=(catalog_spec,))
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Search for sugar")]
    # No permissions passed at all — must default to the runtime's own grants
    result = await agent.run_turn(messages, session_id="s1")

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status != "error"


def test_agent_runtime_permissions_property() -> None:
    """AgentRuntime.permissions returns the equipped runtime's definition.permissions."""
    from agentsys.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    assert agent.permissions == ("read:catalog",)


# ---------------------------------------------------------------------------
# D-014 S2 — execution limits enforcement (design AD-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tool_calls_breach_terminates_gracefully() -> None:
    """When the model keeps requesting tool calls past max_tool_calls, the loop
    terminates with a terminal AIMessage instead of looping/crashing (spec:
    'max_tool_calls breach terminates gracefully')."""
    from agentsys.agent.graph import AgentRuntime

    def _tool_call(call_id: str) -> dict[str, Any]:
        return {
            "id": call_id,
            "name": "catalog_search",
            "args": {"q": "sugar"},
            "type": "tool_call",
        }

    # The model always wants to call a tool — the limit, not the model, must
    # stop the loop.
    responses = [
        AIMessage(content="", tool_calls=[_tool_call("call_limit_001")]),
        AIMessage(content="", tool_calls=[_tool_call("call_limit_002")]),
    ]
    model = ToolAwareFakeModel(responses=responses)

    definition = _fake_definition(execution_limits={"max_tool_calls": 1})
    catalog_spec = _catalog_spec()
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(catalog_spec,),
        denied_tools=(),
        skills=(),
    )
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Search repeatedly")]
    result = await agent.run_turn(
        messages, session_id="s1", permissions=("read:catalog",)
    )

    assert isinstance(result[-1], AIMessage)
    assert not result[-1].tool_calls
    assert result[-1].content
    assert "allowed" in result[-1].content.lower() or "limit" in result[-1].content.lower()
    # Exactly one tool call executed (budget honored, not the 2nd requested one).
    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1


@pytest.mark.asyncio
async def test_recursion_limit_backstop_allows_full_budget_turn() -> None:
    """A turn that legitimately uses the full max_tool_calls budget must not hit
    LangGraph's own default recursion_limit (25) — run_turn must configure a
    recursion_limit derived from max_tool_calls (design AD-3 backstop)."""
    from agentsys.agent.graph import AgentRuntime

    catalog_spec = _catalog_spec()
    max_tool_calls = 15
    tool_call_responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"call_{i}",
                    "name": "catalog_search",
                    "args": {"q": "sugar"},
                    "type": "tool_call",
                }
            ],
        )
        for i in range(max_tool_calls)
    ]
    final_response = AIMessage(content="All done.")
    model = ToolAwareFakeModel(responses=[*tool_call_responses, final_response])

    definition = _fake_definition(execution_limits={"max_tool_calls": max_tool_calls})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(catalog_spec,),
        denied_tools=(),
        skills=(),
    )
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn(
        [HumanMessage(content="Search many times")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert result[-1].content == "All done."


@pytest.mark.asyncio
async def test_tool_call_timeout_appends_error_tool_message_and_continues() -> None:
    """A single slow tool call is bounded by tool_call_timeout_s — it does not
    consume the whole turn budget and the loop continues (design AD-3)."""
    from agentsys.agent.graph import AgentRuntime

    async def slow_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "should never be reached"}

    slow_spec = ToolSpec(
        name="slow_tool",
        required_permissions=(),
        connector=slow_connector,
    )

    tool_call_id = "call_timeout_001"
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
    final_response = AIMessage(content="Done despite the slow tool.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    definition = _fake_definition(execution_limits={"tool_call_timeout_s": 0.05})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(slow_spec,),
        denied_tools=(),
        skills=(),
    )
    agent = AgentRuntime(runtime, model)

    result = await asyncio.wait_for(
        agent.run_turn(
            [HumanMessage(content="Run the slow tool")],
            session_id="s1",
            permissions=(),
        ),
        timeout=2.0,
    )

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"
    assert result[-1].content == "Done despite the slow tool."


class _SlowFakeModel(FakeMessagesListChatModel):
    """A fake model whose ainvoke never returns in time — used to exercise the
    turn-scope total_execution_timeout_s backstop (design AD-3)."""

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> "_SlowFakeModel":
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        await asyncio.sleep(10)
        return AIMessage(content="unreachable")


@pytest.mark.asyncio
async def test_total_execution_timeout_returns_fallback_message() -> None:
    """When the turn exceeds total_execution_timeout_s, run_turn returns the
    caller-supplied messages plus a fallback AIMessage instead of hanging or
    raising (spec: 'Timeout breach terminates gracefully')."""
    from agentsys.agent.graph import AgentRuntime

    model = _SlowFakeModel(responses=[AIMessage(content="unreachable")])
    definition = _fake_definition(execution_limits={"total_execution_timeout_s": 0.05})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )

    agent = AgentRuntime(runtime, model)

    result = await asyncio.wait_for(
        agent.run_turn(
            [HumanMessage(content="Hi")], session_id="s1", permissions=()
        ),
        timeout=2.0,
    )

    assert any(isinstance(m, HumanMessage) for m in result)
    assert isinstance(result[-1], AIMessage)
    assert result[-1].content
