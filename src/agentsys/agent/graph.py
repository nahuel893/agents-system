"""AgentRuntime — LangGraph-based, provider-agnostic agent loop (D-007).

Turns a static EquippedRuntime into a live multi-turn agent via a 2-node
LangGraph graph:
  - call_model  : invokes the bound model with the current message history
  - execute_tools: dispatches each tool_call through the Layer-2 interceptor

The loop continues until the model produces an AIMessage with no tool_calls.
Sync connector functions are wrapped with asyncio.to_thread so they never
block the event loop.

State is NOT persisted internally. The caller supplies the full message history
per turn and owns cross-turn durability (D-008 will add checkpointing).
"""
from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.graph import END, StateGraph

from agentsys.agent.state import AgentState
from agentsys.harness.factory import EquippedRuntime
from agentsys.harness.interceptor import CallResult, PolicyViolation, intercept

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def _call_model(state: AgentState, bound_model: Any) -> dict[str, Any]:
    """Invoke the bound model with the current message history."""
    response: AIMessage = await bound_model.ainvoke(state["messages"])
    logger.info("runtime.model_response", tool_calls=len(response.tool_calls or []))
    return {"messages": [response]}


async def _execute_tools(
    state: AgentState,
    equipped: EquippedRuntime,
    permissions: tuple[str, ...],
) -> dict[str, Any]:
    """Execute all tool_calls in the last AIMessage through the Layer-2 interceptor."""
    last_message = state["messages"][-1]
    tool_calls: list[dict[str, Any]] = getattr(last_message, "tool_calls", []) or []

    result_messages: list[ToolMessage] = []
    for call in tool_calls:
        tool_name: str = call["name"]
        tool_args: dict[str, Any] = call.get("args", {}) or {}
        call_id: str = call["id"]

        try:
            outcome: CallResult = await asyncio.to_thread(
                intercept,
                tool_name,
                tool_args,
                equipped,
                current_permissions=permissions,
            )
            output = outcome.output
            content = (
                json.dumps(output) if isinstance(output, (dict, list)) else str(output)
            )
            result_messages.append(
                ToolMessage(content=content, tool_call_id=call_id)
            )
            logger.info("runtime.tool_executed", tool=tool_name)
        except PolicyViolation as violation:
            result_messages.append(
                ToolMessage(
                    content=f"Tool call blocked: {violation.reason}",
                    tool_call_id=call_id,
                    status="error",
                )
            )
            logger.warning(
                "runtime.tool_blocked",
                tool=tool_name,
                reason=violation.reason,
            )

    return {"messages": result_messages}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _route(state: AgentState) -> str:
    """Route to execute_tools if the last message has tool_calls, else END."""
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", None)
    if tool_calls:
        return "execute_tools"
    return END


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def _build_graph(
    equipped: EquippedRuntime,
    bound_model: Any,
    permissions: tuple[str, ...],
) -> StateGraph[AgentState]:
    """Build the StateGraph with call_model and execute_tools nodes."""
    graph = StateGraph(AgentState)

    graph.add_node(
        "call_model",
        partial(_call_model, bound_model=bound_model),
    )
    graph.add_node(
        "execute_tools",
        partial(_execute_tools, equipped=equipped, permissions=permissions),
    )

    graph.set_entry_point("call_model")
    graph.add_conditional_edges("call_model", _route)
    graph.add_edge("execute_tools", "call_model")

    return graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AgentRuntime:
    """Provider-agnostic, async-native agent runtime.

    Parameters
    ----------
    runtime:
        The fully assembled EquippedRuntime (tools surface, system prompt, etc.).
    model:
        Any LangChain BaseChatModel instance. The runtime binds the granted tool
        surface to it at construction time. Swapping providers requires only
        changing this argument — no other runtime code changes.
    """

    def __init__(self, runtime: EquippedRuntime, model: BaseChatModel) -> None:
        self._runtime = runtime
        self._equipped = runtime
        self._schemas = [spec.to_langchain_tool_schema() for spec in runtime.tools]
        # Only call bind_tools when there are tools to bind — some fake models
        # raise NotImplementedError for bind_tools even with an empty list.
        self._bound_model = model.bind_tools(self._schemas) if self._schemas else model
        logger.info(
            "runtime.initialized",
            tools=len(self._schemas),
            model_type=type(model).__name__,
        )

    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...],
    ) -> list[AnyMessage]:
        """Execute one conversational turn.

        The runtime is stateless: it does NOT persist messages between calls.
        The caller is responsible for maintaining and supplying the full message
        history across turns.

        Parameters
        ----------
        messages:
            Full message history for this turn (including prior context if any).
        session_id:
            Logical session identifier (passed through, stored in AgentState for
            future checkpointer integration in D-008).
        permissions:
            The caller's current permission grants used by the Layer-2 interceptor
            to validate sensitive tool calls at execution time.

        Returns
        -------
        list[AnyMessage]
            All messages accumulated during this turn (input + model responses +
            tool messages). The caller owns cross-turn aggregation.
        """
        compiled = _build_graph(self._equipped, self._bound_model, permissions).compile()
        initial_state: AgentState = {
            "messages": list(messages),
            "session_id": session_id,
            "current_permissions": permissions,
        }
        result = await compiled.ainvoke(initial_state)
        return list(result["messages"])
