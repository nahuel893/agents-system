"""Tests for AgentRuntime — D-007.

These tests exercise the LangGraph-based runtime without real LLM providers:
all model responses come from FakeMessagesListChatModel (sequential responses).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import redis.exceptions
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolSpec

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
    ) -> ToolAwareFakeModel:
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_definition(
    execution_limits: Mapping[str, Any] | None = None,
    untrusted_input: bool = False,
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
        untrusted_input=untrusted_input,
    )


def _make_runtime(
    tools: tuple[ToolSpec, ...] = (),
    untrusted_input: bool = False,
) -> EquippedRuntime:
    return EquippedRuntime(
        definition=_fake_definition(untrusted_input=untrusted_input),
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
        tier=Tier.T1,
        description="Search catalog",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


# ---------------------------------------------------------------------------
# Phase 3 — RED tests (import from graph.py which doesn't exist yet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_returns_final_ai_message() -> None:
    """A turn with no tool calls ends immediately with an AIMessage."""
    from agents_system.agent.graph import AgentRuntime

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
    from agents_system.agent.graph import AgentRuntime

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
    from agents_system.agent.graph import AgentRuntime

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
    from agents_system.agent.graph import AgentRuntime

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
        tier=Tier.T0,
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
    from agents_system.agent.graph import AgentRuntime

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
    from agents_system.agent.graph import AgentRuntime

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
    turn_2_human_contents = [m.content for m in result_2 if isinstance(m, HumanMessage)]
    assert all("Turn 1" not in c for c in turn_2_human_contents)


# ---------------------------------------------------------------------------
# D-009 RED tests — session_provider on EquippedRuntime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_passed_to_async_connector() -> None:
    """session_provider on EquippedRuntime opens a session; async connector receives it."""
    from unittest.mock import AsyncMock, MagicMock

    from agents_system.agent.graph import AgentRuntime

    received_sessions: list[Any] = []

    async def async_catalog(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        received_sessions.append(session)
        return {"results": []}

    async_spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=async_catalog,
        tier=Tier.T1,
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
    from agents_system.agent.graph import AgentRuntime

    received_sessions: list[Any] = []

    async def async_catalog(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        received_sessions.append(session)
        return {"results": []}

    async_spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=async_catalog,
        tier=Tier.T1,
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
async def test_run_turn_permissions_default_to_deploy_grant_ceiling() -> None:
    """permissions=None (default) uses the equipped runtime's persisted
    deploy_grant_ceiling (issue #38) — NOT definition.permissions, the
    role's full declared set. The role below does NOT declare write:orders
    at all, proving the sensitive tool call below cannot be explained by
    the (removed) definition.permissions-based default."""
    from agents_system.agent.graph import AgentRuntime
    from agents_system.permissions import permission_registry

    tool_call_id = "call_perm_001"
    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "order_writer",
                "args": {},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="Order placed.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    def order_writer(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    order_spec = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=order_writer,
        tier=Tier.T2,
    )
    # _fake_definition().permissions == ("read:catalog",) — does NOT include
    # write:orders, so a passing assertion below cannot come from the role's
    # own declared set.
    runtime = EquippedRuntime(
        definition=_fake_definition(),
        system_prompt="You are a helpful assistant.",
        tools=(order_spec,),
        denied_tools=(),
        skills=(),
        deploy_grant_ceiling=frozenset({permission_registry.resolve("write:orders")}),
    )
    agent = AgentRuntime(runtime, model)

    messages = [HumanMessage(content="Place an order")]
    # No permissions passed at all — must default to deploy_grant_ceiling
    result = await agent.run_turn(messages, session_id="s1")

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status != "error"


def test_agent_runtime_permissions_property() -> None:
    """AgentRuntime.permissions returns the equipped runtime's definition.permissions."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    assert agent.permissions == ("read:catalog",)


# ---------------------------------------------------------------------------
# ADR-002 C.13 — AgentRuntime.untrusted_input property
# ---------------------------------------------------------------------------


def test_agent_runtime_untrusted_input_property_reflects_false() -> None:
    """AgentRuntime.untrusted_input returns the equipped runtime's
    definition.untrusted_input when the resolved role is trusted."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime(untrusted_input=False)
    agent = AgentRuntime(runtime, model)

    assert agent.untrusted_input is False


def test_agent_runtime_untrusted_input_property_reflects_true() -> None:
    """Same property, sourced from a role resolved with untrusted_input=True
    — the value create_app's boot-time channel check (ADR-002 C.13) reads."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime(untrusted_input=True)
    agent = AgentRuntime(runtime, model)

    assert agent.untrusted_input is True


# ---------------------------------------------------------------------------
# D-014 S2 — execution limits enforcement (design AD-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tool_calls_breach_terminates_gracefully() -> None:
    """When the model keeps requesting tool calls past max_tool_calls, the loop
    terminates with a terminal AIMessage instead of looping/crashing (spec:
    'max_tool_calls breach terminates gracefully')."""
    from agents_system.agent.graph import AgentRuntime

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
    assert (
        "allowed" in result[-1].content.lower() or "limit" in result[-1].content.lower()
    )
    # Exactly one tool call executed (budget honored, not the 2nd requested one).
    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1


@pytest.mark.asyncio
async def test_recursion_limit_backstop_allows_full_budget_turn() -> None:
    """A turn that legitimately uses the full max_tool_calls budget must not hit
    LangGraph's own default recursion_limit (25) — run_turn must configure a
    recursion_limit derived from max_tool_calls (design AD-3 backstop)."""
    from agents_system.agent.graph import AgentRuntime

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
    from agents_system.agent.graph import AgentRuntime

    async def slow_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "should never be reached"}

    slow_spec = ToolSpec(
        name="slow_tool",
        required_permissions=(),
        connector=slow_connector,
        tier=Tier.T0,
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
    ) -> _SlowFakeModel:
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        await asyncio.sleep(10)
        return AIMessage(content="unreachable")


@pytest.mark.asyncio
async def test_total_execution_timeout_returns_fallback_message() -> None:
    """When the turn exceeds total_execution_timeout_s, run_turn returns the
    caller-supplied messages plus a fallback AIMessage instead of hanging or
    raising (spec: 'Timeout breach terminates gracefully')."""
    from agents_system.agent.graph import AgentRuntime

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
        agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=()),
        timeout=2.0,
    )

    assert any(isinstance(m, HumanMessage) for m in result)
    assert isinstance(result[-1], AIMessage)
    assert result[-1].content


# ---------------------------------------------------------------------------
# D-014 S4 — system prompt moved to call time (design AD-1)
# ---------------------------------------------------------------------------


class _CapturingFakeModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel that records every message list it is invoked
    with, so tests can assert what the MODEL actually received (as opposed to
    what ends up in the returned/persisted message list)."""

    # RUF012 false positive: this is a pydantic BaseModel field (langchain_core's
    # FakeMessagesListChatModel), so pydantic gives each instance its own list --
    # not a shared mutable class default.
    captured_inputs: list[list[Any]] = []  # noqa: RUF012

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _CapturingFakeModel:
        return self

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        self.captured_inputs.append(list(input))
        return await super().ainvoke(input, *args, **kwargs)


@pytest.mark.asyncio
async def test_system_prompt_not_persisted_in_returned_messages() -> None:
    """The runtime SystemMessage must never appear in run_turn's returned list
    (design AD-1) — it is a model-input-only concern, kept out of state so a
    checkpointer never accumulates/duplicates it across turns."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="Hi there!")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn(
        [HumanMessage(content="Hi")], session_id="s1", permissions=()
    )

    assert not any(isinstance(m, SystemMessage) for m in result)


@pytest.mark.asyncio
async def test_system_prompt_injected_at_model_call_time() -> None:
    """_call_model prepends the runtime's system prompt to the MODEL INPUT on
    every call, even though it is never stored in state (design AD-1)."""
    from agents_system.agent.graph import AgentRuntime

    model = _CapturingFakeModel(responses=[AIMessage(content="Hi there!")])
    model.captured_inputs = []
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    await agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=())

    assert len(model.captured_inputs) == 1
    first_call_input = model.captured_inputs[0]
    assert isinstance(first_call_input[0], SystemMessage)
    assert first_call_input[0].content == "You are a helpful assistant."


# ---------------------------------------------------------------------------
# D-014 S4 — checkpointer opt-in (design AD-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_id_none_compiles_without_checkpointer() -> None:
    """thread_id=None (default) — behavior is byte-identical to pre-D-014:
    the graph compiles WITHOUT a checkpointer even when one is configured on
    the runtime (design AD-1: opt-in per invocation, not blanket)."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    checkpointer = InMemorySaver()
    model = FakeMessagesListChatModel(
        responses=[AIMessage(content="First"), AIMessage(content="Second")]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    result_1 = await agent.run_turn(
        [HumanMessage(content="Turn 1")], session_id="s1", permissions=()
    )
    result_2 = await agent.run_turn(
        [HumanMessage(content="Turn 2")], session_id="s2", permissions=()
    )

    # No thread_id was ever passed — no cross-turn bleed, exactly like the
    # stateless adapter path.
    assert result_1[-1].content == "First"
    assert result_2[-1].content == "Second"
    assert not any(
        "Turn 1" in m.content for m in result_2 if isinstance(m, HumanMessage)
    )


@pytest.mark.asyncio
async def test_thread_id_engages_checkpointer_for_cross_turn_retention() -> None:
    """Two run_turn calls with the SAME thread_id retain prior-turn messages
    via the checkpointer (spec: 'Multi-turn context retention')."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    checkpointer = InMemorySaver()
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content="Nice to meet you, Ana."),
            AIMessage(content="Your name is Ana."),
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    await agent.run_turn(
        [HumanMessage(content="My name is Ana")],
        session_id="s1",
        permissions=(),
        thread_id="+5491100000000",
    )
    result_2 = await agent.run_turn(
        [HumanMessage(content="What is my name?")],
        session_id="s2",
        permissions=(),
        thread_id="+5491100000000",
    )

    # Turn 1's HumanMessage must still be present — proves the checkpointer
    # accumulated state across the two calls for the same thread_id.
    human_contents = [m.content for m in result_2 if isinstance(m, HumanMessage)]
    assert "My name is Ana" in human_contents
    assert "What is my name?" in human_contents
    assert result_2[-1].content == "Your name is Ana."


@pytest.mark.asyncio
async def test_tool_call_count_resets_to_zero_on_checkpointer_resume() -> None:
    """A stale persisted tool_call_count must NOT carry over into the next
    turn loaded from a checkpoint — it must start fresh at 0 each turn, even
    though messages accumulate (design AD-1/AD-3)."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    def _tool_call(call_id: str) -> dict[str, Any]:
        return {
            "id": call_id,
            "name": "catalog_search",
            "args": {"q": "sugar"},
            "type": "tool_call",
        }

    checkpointer = InMemorySaver()
    catalog_spec = _catalog_spec()
    definition = _fake_definition(execution_limits={"max_tool_calls": 1})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(catalog_spec,),
        denied_tools=(),
        skills=(),
    )
    # The fake model serves responses sequentially across BOTH run_turn calls
    # below (it has no notion of "turn") — so the response list must account
    # for every call_model invocation in order: turn 1 makes 2 (tool call,
    # then a plain final reply that ends the turn at tool_call_count=1);
    # turn 2 makes 2 more (tool call, then its own final reply) IF AND ONLY
    # IF tool_call_count correctly reset to 0 — otherwise turn 2 would route
    # straight to limit_reached on its first call_model response and never
    # consume the scripted "Done with turn 2." reply.
    model = ToolAwareFakeModel(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("call_t1")]),
            AIMessage(content="Turn 1 done."),
            AIMessage(content="", tool_calls=[_tool_call("call_t2")]),
            AIMessage(content="Done with turn 2."),
        ]
    )
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    result_1 = await agent.run_turn(
        [HumanMessage(content="Search sugar")],
        session_id="s1",
        permissions=("read:catalog",),
        thread_id="+5491100000001",
    )
    assert len([m for m in result_1 if isinstance(m, ToolMessage)]) == 1
    assert result_1[-1].content == "Turn 1 done."

    result_2 = await agent.run_turn(
        [HumanMessage(content="Search sugar again")],
        session_id="s2",
        permissions=("read:catalog",),
        thread_id="+5491100000001",
    )

    tool_messages_turn_2 = [
        m
        for m in result_2
        if isinstance(m, ToolMessage) and m.tool_call_id == "call_t2"
    ]
    assert len(tool_messages_turn_2) == 1
    assert result_2[-1].content == "Done with turn 2."


# ---------------------------------------------------------------------------
# D-014 S4 — checkpointer-failure degradation (design AD-8)
# ---------------------------------------------------------------------------


class _FailingCheckpointer(BaseCheckpointSaver[str]):
    """A checkpointer whose backend read raises redis.ConnectionError — NO
    real network involved, this simulates an unreachable Redis instance."""

    async def aget_tuple(self, config: Any) -> Any:
        raise redis.exceptions.ConnectionError("Redis unavailable (simulated)")


@pytest.mark.asyncio
async def test_checkpointer_failure_degrades_without_crashing_the_turn() -> None:
    """A checkpointer backend failure must NOT crash run_turn — the turn
    completes over the caller-supplied messages only, and the degradation is
    logged (spec: 'Checkpointer unavailable')."""
    from agents_system.agent.graph import AgentRuntime

    checkpointer = _FailingCheckpointer()
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Still here.")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    result = await agent.run_turn(
        [HumanMessage(content="Hello")],
        session_id="s1",
        permissions=(),
        thread_id="+5491100000002",
    )

    assert isinstance(result[-1], AIMessage)
    assert result[-1].content == "Still here."
    # Caller-supplied message is present — the fallback ran over it, not an
    # empty/lost history.
    assert any(isinstance(m, HumanMessage) and m.content == "Hello" for m in result)


@pytest.mark.asyncio
async def test_checkpointer_failure_degradation_is_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The degradation is logged with a reason. This project's structlog setup
    uses PrintLoggerFactory (writes straight to stdout, not routed through the
    stdlib logging module) — so stdout capture, not caplog, is the correct
    assertion mechanism here."""
    from agents_system.agent.graph import AgentRuntime

    checkpointer = _FailingCheckpointer()
    model = FakeMessagesListChatModel(responses=[AIMessage(content="Still here.")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    await agent.run_turn(
        [HumanMessage(content="Hello")],
        session_id="s1",
        permissions=(),
        thread_id="+5491100000003",
    )

    captured = capsys.readouterr()
    assert "runtime.checkpointer_degraded" in captured.out


@pytest.mark.asyncio
async def test_turn_timeout_is_not_mislabeled_as_checkpointer_degradation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A total_execution_timeout_s breach must still be handled by AD-3's own
    fallback (a sendable timeout AIMessage) and must NOT be logged/treated as
    a checkpointer degradation — the two failure classes are distinct even
    when a checkpointer is engaged (and healthy) for this turn."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    checkpointer = InMemorySaver()
    definition = _fake_definition(execution_limits={"total_execution_timeout_s": 0.05})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )
    agent = AgentRuntime(
        runtime,
        _SlowFakeModel(responses=[AIMessage(content="unreachable")]),
        checkpointer=checkpointer,
    )

    result = await asyncio.wait_for(
        agent.run_turn(
            [HumanMessage(content="Hi")],
            session_id="s1",
            permissions=(),
            thread_id="+5491100000004",
        ),
        timeout=2.0,
    )

    assert isinstance(result[-1], AIMessage)
    assert result[-1].content
    captured = capsys.readouterr()
    assert "checkpointer_degraded" not in captured.out


# ---------------------------------------------------------------------------
# #78 Phase 0 — real per-turn token usage and cost
# ---------------------------------------------------------------------------


async def test_run_turn_sums_usage_metadata_across_model_calls() -> None:
    """A turn that makes two model calls (a tool round-trip) sums BOTH
    calls' real usage_metadata into `run_turn_with_usage`'s returned
    `TurnResult.usage` -- never just the last call's numbers. Review finding
    5 (PR #87): `run_turn_with_usage` is the explicit, type-safe way to get
    `.usage` (replaces the earlier `TurnMessages` list subclass); plain
    `run_turn` still returns a real `list[AnyMessage]` unmodified."""
    from agents_system.agent.graph import AgentRuntime

    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_usage_001",
                "name": "catalog_search",
                "args": {},
                "type": "tool_call",
            }
        ],
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    final_response = AIMessage(
        content="Here you go.",
        usage_metadata={"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
    )
    model = ToolAwareFakeModel(responses=[first_response, final_response])
    runtime = _make_runtime(tools=(_catalog_spec(),))
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1"
    )

    assert isinstance(result.messages, list)
    assert any(isinstance(m, ToolMessage) for m in result.messages)
    assert result.usage.model_calls == 2
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 50
    assert result.usage.total_tokens == 300
    # No configured price for this fake model's derived id -> cost is
    # honestly None, never a guessed number.
    assert result.usage.cost_usd is None


async def test_run_turn_usage_is_none_when_a_model_call_reports_none() -> None:
    """Honesty rule: if even one of this turn's model calls reports no
    usage_metadata, the turn's token totals are None -- never a partial
    guess (TurnUsage's own documented contract)."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1"
    )

    assert result.usage.model_calls == 1
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None


async def test_run_turn_usage_is_unknown_on_timeout() -> None:
    """A turn that times out cannot know how many model calls it actually
    completed -- model_calls is None (genuinely unknown), never a
    misleading 0."""
    from agents_system.agent.graph import AgentRuntime

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
        agent.run_turn_with_usage(
            [HumanMessage(content="Hi")], session_id="s1", permissions=()
        ),
        timeout=2.0,
    )

    assert result.usage.model_calls is None
    assert result.usage.total_tokens is None
    assert result.usage.cost_usd is None


async def test_run_turn_computes_cost_from_configured_price_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cost_usd is computed only from Settings.model_prices, keyed by the
    model_id run_turn was given -- never a hardcoded rate."""
    from agents_system.agent import graph as graph_module
    from agents_system.agent.graph import AgentRuntime
    from agents_system.config import ModelPrice, Settings

    priced_settings = Settings(
        _env_file=None,
        model_prices={
            "acme__sales-agent": ModelPrice(
                input_per_million=1.0, output_per_million=2.0
            )
        },
    )
    monkeypatch.setattr(graph_module, "get_settings", lambda: priced_settings)

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 1_000_000,
                    "output_tokens": 500_000,
                    "total_tokens": 1_500_000,
                },
            )
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1", model_id="acme__sales-agent"
    )

    # 1_000_000 input tokens @ $1/M + 500_000 output tokens @ $2/M = $1 + $1
    assert result.usage.cost_usd == pytest.approx(2.0)


async def test_run_turn_cost_is_none_without_a_configured_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model_id with no entry in Settings.model_prices yields cost_usd=None
    -- a missing price is never guessed at."""
    from agents_system.agent import graph as graph_module
    from agents_system.agent.graph import AgentRuntime
    from agents_system.config import Settings

    monkeypatch.setattr(graph_module, "get_settings", lambda: Settings(_env_file=None))

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            )
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1", model_id="unpriced-model"
    )

    assert result.usage.cost_usd is None


# ---------------------------------------------------------------------------
# #78 Phase 0 review finding 4 -- AgentRuntime derives its own default price
# key from the model it was constructed with (model_display_name), so a
# caller that never names a model id still gets priced correctly.
# ---------------------------------------------------------------------------


async def test_run_turn_prices_under_the_runtimes_own_derived_model_id_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 4 (PR #87) -- no `model_id` override given at all: the
    runtime must still price this turn's usage, keyed by
    `model_display_name(model)` (this fake model exposes no `model_name`/
    `model` attribute, so its derived id is its class name). This is what
    fixes the WhatsApp webhook worker, which never passes `model_id`."""
    from agents_system.agent import graph as graph_module
    from agents_system.agent.graph import AgentRuntime, model_display_name
    from agents_system.config import ModelPrice, Settings

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "total_tokens": 1_000_000,
                },
            )
        ]
    )
    derived_id = model_display_name(model)
    priced_settings = Settings(
        _env_file=None,
        model_prices={
            derived_id: ModelPrice(input_per_million=3.0, output_per_million=0.0)
        },
    )
    monkeypatch.setattr(graph_module, "get_settings", lambda: priced_settings)

    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1"
    )

    assert result.usage.cost_usd == pytest.approx(3.0)


async def test_run_turn_explicit_model_id_overrides_the_derived_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 4 (PR #87) -- an explicit `model_id` argument still
    wins over the runtime's own derived default (e.g. a live-eval comparing
    several runtime configurations under one shared label)."""
    from agents_system.agent import graph as graph_module
    from agents_system.agent.graph import AgentRuntime
    from agents_system.config import ModelPrice, Settings

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "total_tokens": 1_000_000,
                },
            )
        ]
    )
    priced_settings = Settings(
        _env_file=None,
        model_prices={
            "explicit-override": ModelPrice(
                input_per_million=5.0, output_per_million=0.0
            )
        },
    )
    monkeypatch.setattr(graph_module, "get_settings", lambda: priced_settings)

    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1", model_id="explicit-override"
    )

    assert result.usage.cost_usd == pytest.approx(5.0)


async def test_run_turn_logs_one_turn_usage_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exactly one structured `runtime.turn_usage` log event is emitted per
    turn, carrying the usage and cost together (#78 Phase 0)."""
    from agents_system.agent.graph import AgentRuntime

    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            )
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    await agent.run_turn([HumanMessage(content="hi")], session_id="s1")

    captured = capsys.readouterr()
    # This project's structlog setup (PrintLoggerFactory + ConsoleRenderer in
    # tests -- see test_checkpointer_failure_degradation_is_logged's own
    # docstring) writes key=value pairs straight to stdout, not JSON; assert
    # on that shape, same as the other structlog assertions in this file.
    usage_lines = [
        line for line in captured.out.splitlines() if "runtime.turn_usage" in line
    ]
    assert len(usage_lines) == 1
    assert "total_tokens=15" in usage_lines[0]
    assert "model_calls=1" in usage_lines[0]


# ---------------------------------------------------------------------------
# #78 Phase 0 review finding 3 -- retries must not silently under-report
# ---------------------------------------------------------------------------


class _FailsOnceThenSucceedsModel(FakeMessagesListChatModel):
    """Raises a tool-format error on its FIRST `ainvoke`, then serves its
    scripted `responses` normally -- `.bound` returns self so `_call_model`'s
    `base_model = bound_model.bound` fallback path works the same way a real
    bound model's `.bound` (the unwrapped base) would."""

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _FailsOnceThenSucceedsModel:
        return self

    @property
    def bound(self) -> _FailsOnceThenSucceedsModel:
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        if not getattr(self, "_failed_once", False):
            object.__setattr__(self, "_failed_once", True)
            raise ValueError("tool_use_failed: malformed tool call")
        return await super().ainvoke(*args, **kwargs)


async def test_tool_format_error_retry_accounts_for_both_calls_and_nulls_usage() -> (
    None
):
    """Review finding 3 (PR #87) -- reproduces review probe
    `probe_retries.py`'s first scenario. The tool-format-error retry in
    `_call_model` makes TWO real provider calls (the first one raises after
    already consuming tokens; only the fallback succeeds). `model_calls` must
    count both, and the turn's token totals must be honestly `None` (the
    failed first attempt's tokens are unrecoverable) -- never silently
    reporting only the fallback call's numbers as if it were the whole
    turn."""
    from agents_system.agent.graph import AgentRuntime

    model = _FailsOnceThenSucceedsModel(
        responses=[
            AIMessage(
                content="Plain fallback response",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            )
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")], session_id="s1"
    )

    assert result.messages[-1].content == "Plain fallback response"
    assert result.usage.model_calls == 2
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None


async def test_checkpointer_degradation_marks_turn_usage_fully_unknown() -> None:
    """Review finding 3 (PR #87) -- reproduces review probe
    `probe_retries.py`'s second scenario. A checkpointer backend failure that
    surfaces AFTER a model call already completed (simulated here via a
    checkpointer whose `aput`/`put` raises, not `aget_tuple`, matching the
    review's own description: 'When Redis fails during checkpoint save')
    forces a same-turn retry WITHOUT the checkpointer. The failed attempt's
    tokens are unrecoverable, so the whole turn's usage must be reported
    fully unknown (`model_calls=None`) -- never the retried invocation's own
    count reported as if the failed attempt spent nothing."""
    from langgraph.checkpoint.base import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
    )
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    class _FailsOnFirstPutCheckpointer(InMemorySaver):
        """A real, working InMemorySaver whose FIRST `aput` raises a
        redis.ConnectionError -- simulating a Redis backend failure that
        happens while saving the checkpoint AFTER a model call completed
        (not while reading the initial checkpoint, unlike this file's
        existing `_FailingCheckpointer`)."""

        def __init__(self) -> None:
            super().__init__()
            self._put_calls = 0

        async def aput(
            self,
            config: Any,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
        ) -> Any:
            self._put_calls += 1
            if self._put_calls == 1:
                raise redis.exceptions.ConnectionError(
                    "Redis connection lost during checkpoint put (simulated)"
                )
            return await super().aput(config, checkpoint, metadata, new_versions)

    checkpointer = _FailsOnFirstPutCheckpointer()
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="Still here.",
                usage_metadata={
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "total_tokens": 60,
                },
            )
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="hi")],
        session_id="s1",
        thread_id="+5491100000005",
    )

    assert result.messages[-1].content == "Still here."
    assert result.usage.model_calls is None
    assert result.usage.total_tokens is None
    assert result.usage.cost_usd is None


# ---------------------------------------------------------------------------
# #78 Phase 0 review finding 7 -- missing test coverage the review flagged
# ---------------------------------------------------------------------------


async def test_two_turn_thread_usage_does_not_double_count_turn_one() -> None:
    """Review finding 7 (PR #87) -- a 2-turn conversation sharing a
    `thread_id` must report turn 2's usage as ONLY turn 2's own model
    call(s), never turn 1's usage bleeding in cumulatively (AgentState's
    `turn_usage` resets to `[]` in `initial_state` every call -- this test
    formalizes that behavior, previously verified only by an inline probe,
    not by CI)."""
    from langgraph.checkpoint.memory import InMemorySaver

    from agents_system.agent.graph import AgentRuntime

    checkpointer = InMemorySaver()
    model = FakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="First turn reply",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            ),
            AIMessage(
                content="Second turn reply",
                usage_metadata={
                    "input_tokens": 30,
                    "output_tokens": 5,
                    "total_tokens": 35,
                },
            ),
        ]
    )
    runtime = _make_runtime()
    agent = AgentRuntime(runtime, model, checkpointer=checkpointer)

    result_1 = await agent.run_turn_with_usage(
        [HumanMessage(content="Turn 1")],
        session_id="s1",
        thread_id="+5491100000006",
    )
    result_2 = await agent.run_turn_with_usage(
        [HumanMessage(content="Turn 2")],
        session_id="s2",
        thread_id="+5491100000006",
    )

    assert result_1.usage.total_tokens == 120
    # Turn 2's usage must be ONLY turn 2's own call -- not 120 + 35 = 155.
    assert result_2.usage.total_tokens == 35
    assert result_2.usage.model_calls == 1


async def test_limit_reached_message_not_counted_in_usage() -> None:
    """Review finding 7 (PR #87) -- when a turn ends at the `_limit_reached`
    terminal node, its fixed `AIMessage` is appended WITHOUT going through
    `_call_model` (no provider call is made for it), so it must not inflate
    `TurnUsage.model_calls` or be treated as a `None` usage_metadata entry
    that would null the turn's honest totals.

    With `max_tool_calls=1`, the graph legitimately makes TWO real
    `call_model` invocations before the SECOND one's tool-call request is
    redirected to `_limit_reached` instead of `execute_tools` (`_route`
    checks `tool_call_count` AFTER that second response already exists --
    same shape as this file's existing
    `test_max_tool_calls_breach_terminates_gracefully`). Both of those real
    calls must be counted; only the terminal node's OWN synthetic message
    (appended without any further `call_model` call) must not add a third."""
    from agents_system.agent.graph import AgentRuntime

    def _tool_call(call_id: str) -> dict[str, Any]:
        return {
            "id": call_id,
            "name": "catalog_search",
            "args": {"q": "sugar"},
            "type": "tool_call",
        }

    responses = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("call_limit_usage_001")],
            usage_metadata={
                "input_tokens": 40,
                "output_tokens": 10,
                "total_tokens": 50,
            },
        ),
        AIMessage(
            content="",
            tool_calls=[_tool_call("call_limit_usage_002")],
            usage_metadata={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
        ),
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

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="Search repeatedly")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert "allowed" in result.messages[-1].content.lower()
    # Exactly the TWO real call_model invocations above are counted -- the
    # limit_reached node's own fixed message adds no third entry.
    assert result.usage.model_calls == 2
    assert result.usage.input_tokens == 55
    assert result.usage.output_tokens == 15
    assert result.usage.total_tokens == 70


# ---------------------------------------------------------------------------
# #78 Phase 0 review follow-up (PR #87) -- a hostile or compromised
# "openai_compatible" backend (an explicitly supported, operator-selectable
# third-party/self-hosted endpoint) can return a syntactically valid but
# absurdly large prompt_tokens/completion_tokens value: JSON integers have no
# size ceiling, and LangChain's UsageMetadata does not bound them.
# _compute_turn_cost must never raise on it -- an implausible count is
# untrustworthy, so cost_usd is honestly None, the same as a missing price.
# ---------------------------------------------------------------------------


def test_compute_turn_cost_returns_none_for_implausibly_large_token_counts() -> None:
    """A huge attacker/compromised-backend-supplied input_tokens value must
    not raise OverflowError out of _compute_turn_cost."""
    from agents_system.agent.graph import TurnUsage, _compute_turn_cost
    from agents_system.config import ModelPrice, Settings

    malicious_usage = TurnUsage(
        model_calls=1,
        input_tokens=10**320,
        output_tokens=10,
        total_tokens=10**320 + 10,
    )
    settings = Settings(
        _env_file=None,
        model_prices={
            "evil-backend-model": ModelPrice(
                input_per_million=0.14, output_per_million=0.28
            )
        },
    )

    cost = _compute_turn_cost(malicious_usage, "evil-backend-model", settings)

    assert cost is None


def test_compute_turn_cost_returns_none_for_negative_token_counts() -> None:
    """A negative token count is equally untrustworthy -- it must never be
    fed into the cost formula either."""
    from agents_system.agent.graph import TurnUsage, _compute_turn_cost
    from agents_system.config import ModelPrice, Settings

    usage = TurnUsage(model_calls=1, input_tokens=-5, output_tokens=10, total_tokens=5)
    settings = Settings(
        _env_file=None,
        model_prices={
            "some-model": ModelPrice(input_per_million=1.0, output_per_million=1.0)
        },
    )

    assert _compute_turn_cost(usage, "some-model", settings) is None


def test_compute_turn_cost_still_prices_ordinary_plausible_usage() -> None:
    """Sanity check: the new bounds check does not disturb an ordinary
    turn's cost computation."""
    from agents_system.agent.graph import TurnUsage, _compute_turn_cost
    from agents_system.config import ModelPrice, Settings

    usage = TurnUsage(
        model_calls=1,
        input_tokens=1_000_000,
        output_tokens=500_000,
        total_tokens=1_500_000,
    )
    settings = Settings(
        _env_file=None,
        model_prices={
            "some-model": ModelPrice(input_per_million=1.0, output_per_million=2.0)
        },
    )

    assert _compute_turn_cost(usage, "some-model", settings) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# #78 Phase 0 Slice 2 -- turn/tool-call/limit-trip metrics recorded at the
# existing choke points (run_turn_with_usage/_finish_turn, _execute_tools,
# _limit_reached). Every test builds its OWN Metrics from a fresh
# CollectorRegistry (never DEFAULT_METRICS) so tests never collide with
# each other -- see tests/test_observability_metrics.py's module docstring.
# ---------------------------------------------------------------------------


def _fresh_metrics() -> Any:
    from prometheus_client import CollectorRegistry

    from agents_system.observability.metrics import build_metrics

    return build_metrics(CollectorRegistry())


@pytest.mark.asyncio
async def test_ok_turn_records_turn_metrics_and_token_cost() -> None:
    """A normal turn increments turns_total{outcome="ok"}, observes
    turn_duration_seconds, and (usage being known) tokens_total/cost_usd_total."""
    from agents_system.agent.graph import AgentRuntime

    final_reply = AIMessage(
        content="Hello!",
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )
    model = FakeMessagesListChatModel(responses=[final_reply])
    runtime = _make_runtime()
    metrics = _fresh_metrics()
    agent = AgentRuntime(
        runtime, model, runtime_id="acme__sales-agent", metrics=metrics
    )

    await agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=())

    assert (
        metrics.turns_total.labels(
            runtime_id="acme__sales-agent", outcome="ok"
        )._value.get()
        == 1
    )
    histogram = metrics.turn_duration_seconds.labels(
        runtime_id="acme__sales-agent", outcome="ok"
    )
    assert histogram._sum.get() >= 0
    assert (
        metrics.tokens_total.labels(
            runtime_id="acme__sales-agent", direction="input"
        )._value.get()
        == 10
    )
    assert (
        metrics.tokens_total.labels(
            runtime_id="acme__sales-agent", direction="output"
        )._value.get()
        == 4
    )


@pytest.mark.asyncio
async def test_runtime_id_defaults_to_derived_model_id_when_not_given() -> None:
    """Constructing an AgentRuntime with no explicit runtime_id labels its
    metrics under its own derived provider model id (backward-compatible
    default -- every existing construction site keeps working unmodified)."""
    from agents_system.agent.graph import AgentRuntime, model_display_name

    model = FakeMessagesListChatModel(responses=[AIMessage(content="hi")])
    runtime = _make_runtime()
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    await agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=())

    expected_runtime_id = model_display_name(model)
    assert (
        metrics.turns_total.labels(
            runtime_id=expected_runtime_id, outcome="ok"
        )._value.get()
        == 1
    )


@pytest.mark.asyncio
async def test_timeout_turn_records_timeout_outcome_with_no_token_counters() -> None:
    """A turn that hits total_execution_timeout_s records outcome="timeout"
    and increments no tokens_total/cost_usd_total sample (usage is honestly
    unknown -- see TurnUsage's own honesty rule)."""
    from agents_system.agent.graph import AgentRuntime

    model = _SlowFakeModel(responses=[AIMessage(content="unreachable")])
    definition = _fake_definition(execution_limits={"total_execution_timeout_s": 0.05})
    runtime = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )
    metrics = _fresh_metrics()
    agent = AgentRuntime(
        runtime, model, runtime_id="acme__sales-agent", metrics=metrics
    )

    await asyncio.wait_for(
        agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=()),
        timeout=2.0,
    )

    assert (
        metrics.turns_total.labels(
            runtime_id="acme__sales-agent", outcome="timeout"
        )._value.get()
        == 1
    )
    families = {f.name: f for f in metrics.registry.collect()}
    assert families["agent_tokens"].samples == []
    assert families["agent_cost_usd"].samples == []


@pytest.mark.asyncio
async def test_ok_tool_call_records_ok_outcome() -> None:
    from agents_system.agent.graph import AgentRuntime

    tool_call_id = "call_metrics_ok"
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
    final_response = AIMessage(content="Done.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    catalog_spec = _catalog_spec()
    runtime = _make_runtime(tools=(catalog_spec,))
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    await agent.run_turn(
        [HumanMessage(content="Search for sugar")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert (
        metrics.tool_calls_total.labels(
            tool="catalog_search", outcome="ok"
        )._value.get()
        == 1
    )


@pytest.mark.asyncio
async def test_tool_call_to_unknown_tool_records_denied_outcome() -> None:
    """PolicyViolation(reason="not_in_surface") -- a tool never equipped at
    all -- is recorded as outcome="denied"."""
    from agents_system.agent.graph import AgentRuntime

    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_metrics_denied",
                "name": "nonexistent_tool",
                "args": {},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="I cannot use that tool.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    runtime = _make_runtime()  # empty surface
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    await agent.run_turn([HumanMessage(content="Hi")], session_id="s1", permissions=())

    assert (
        metrics.tool_calls_total.labels(
            tool="nonexistent_tool", outcome="denied"
        )._value.get()
        == 1
    )


@pytest.mark.asyncio
async def test_tool_call_revalidation_failure_records_blocked_outcome() -> None:
    """PolicyViolation(reason="permission_revoked") -- a sensitive tool that
    WAS equipped but fails Layer-2 revalidation -- is recorded as
    outcome="blocked", distinct from "denied" above."""
    from agents_system.agent.graph import AgentRuntime

    def write_order(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"order_id": "ord-1"}

    sensitive_spec = ToolSpec(
        name="write_order",
        required_permissions=("write:orders",),
        connector=write_order,
        tier=Tier.T2,
    )

    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_metrics_blocked",
                "name": "write_order",
                "args": {},
                "type": "tool_call",
            }
        ],
    )
    final_response = AIMessage(content="Could not write the order.")
    model = ToolAwareFakeModel(responses=[first_response, final_response])

    runtime = _make_runtime(tools=(sensitive_spec,))
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    # Layer-2 revalidation: current_permissions no longer grants write:orders
    # (narrowed after equipping) -- required_permissions coverage fails.
    await agent.run_turn(
        [HumanMessage(content="Place the order")], session_id="s1", permissions=()
    )

    assert (
        metrics.tool_calls_total.labels(
            tool="write_order", outcome="blocked"
        )._value.get()
        == 1
    )


@pytest.mark.asyncio
async def test_slow_tool_call_records_timeout_outcome() -> None:
    from agents_system.agent.graph import AgentRuntime

    async def slow_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"status": "should never be reached"}

    slow_spec = ToolSpec(
        name="slow_tool",
        required_permissions=(),
        connector=slow_connector,
        tier=Tier.T0,
    )
    first_response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_metrics_timeout",
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
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    await asyncio.wait_for(
        agent.run_turn(
            [HumanMessage(content="Run the slow tool")],
            session_id="s1",
            permissions=(),
        ),
        timeout=2.0,
    )

    assert (
        metrics.tool_calls_total.labels(
            tool="slow_tool", outcome="timeout"
        )._value.get()
        == 1
    )


@pytest.mark.asyncio
async def test_max_tool_calls_breach_records_limit_trip() -> None:
    from agents_system.agent.graph import AgentRuntime

    def _tool_call(call_id: str) -> dict[str, Any]:
        return {
            "id": call_id,
            "name": "catalog_search",
            "args": {"q": "sugar"},
            "type": "tool_call",
        }

    responses = [
        AIMessage(content="", tool_calls=[_tool_call("call_limit_metrics_001")]),
        AIMessage(content="", tool_calls=[_tool_call("call_limit_metrics_002")]),
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
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    await agent.run_turn(
        [HumanMessage(content="Search repeatedly")],
        session_id="s1",
        permissions=("read:catalog",),
    )

    assert metrics.limit_trips_total.labels(limit="max_tool_calls")._value.get() == 1


@pytest.mark.asyncio
async def test_session_id_never_reaches_exported_metrics_text() -> None:
    """Label hygiene, end to end: a session_id shaped like a phone number
    must never appear in this runtime's recorded metrics."""
    from prometheus_client import generate_latest

    from agents_system.agent.graph import AgentRuntime

    final_reply = AIMessage(content="Hello!")
    model = FakeMessagesListChatModel(responses=[final_reply])
    runtime = _make_runtime()
    metrics = _fresh_metrics()
    agent = AgentRuntime(runtime, model, metrics=metrics)

    phone_like_session_id = "+5491112345678"
    await agent.run_turn(
        [HumanMessage(content="Hi")],
        session_id=phone_like_session_id,
        permissions=(),
    )

    output = generate_latest(metrics.registry).decode()
    assert phone_like_session_id not in output
