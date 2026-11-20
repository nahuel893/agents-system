"""Building the agent graph: the routing between its nodes and the compiled StateGraph."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from agents_system.agent.limits import (
    _LIMIT_REACHED_NODE,
    _TOOL_ERRORS_NODE,
)
from agents_system.agent.nodes.model import (
    BudgetGate,
    _call_model,
)
from agents_system.agent.nodes.tools import (
    _execute_tools,
    _limit_reached,
    _tool_errors_reached,
)
from agents_system.agent.state import AgentState
from agents_system.execution_limits import (
    DEFAULT_TOOL_ERROR_REPLY,
)
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS
from agents_system.observability.metrics import (
    Metrics,
)
from agents_system.scope import ScopeProvider

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _after_tools(state: AgentState, max_tool_errors: int) -> str:
    """Back to the model, unless the turn's tool errors reached the cap (#289)."""
    if state.get("tool_error_count", 0) >= max_tool_errors:
        return _TOOL_ERRORS_NODE
    return "call_model"


def _route(state: AgentState, max_tool_calls: int) -> str:
    """Route to execute_tools if the last message has tool_calls, else END.

    D-014 S2 (design AD-3): if the turn has already used its max_tool_calls
    budget, route to the terminal limit_reached node instead of executing
    another tool call — regardless of what the model just requested.
    """
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", None)
    if not tool_calls:
        return END
    if state.get("tool_call_count", 0) >= max_tool_calls:
        return _LIMIT_REACHED_NODE
    return "execute_tools"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def _build_graph(
    equipped: EquippedRuntime,
    bound_model: Any,
    permissions: tuple[str, ...],
    max_tool_calls: int,
    tool_call_timeout_s: float,
    metrics: Metrics,
    max_context_tokens: int | None = None,
    scope_provider: ScopeProvider | None = None,
    runtime_id: str | None = None,
    budget_gate: BudgetGate | None = None,
    untrusted_text: str = "",
    trusted_context: str = "",
    max_tool_errors: int = PLATFORM_DEFAULT_LIMITS["max_tool_errors"],
    tool_error_reply: str = DEFAULT_TOOL_ERROR_REPLY,
) -> StateGraph[AgentState]:
    """Build the StateGraph with call_model, execute_tools and limit_reached nodes."""
    graph = StateGraph(AgentState)

    graph.add_node(
        "call_model",
        partial(
            _call_model,
            bound_model=bound_model,
            system_prompt=equipped.system_prompt,
            max_context_tokens=max_context_tokens,
            tool_schemas=[spec.to_langchain_tool_schema() for spec in equipped.tools],
            definition=equipped.definition,
            metrics=metrics,
            budget_gate=budget_gate,
            untrusted_text=untrusted_text,
            trusted_context=trusted_context,
        ),
    )
    graph.add_node(
        "execute_tools",
        partial(
            _execute_tools,
            equipped=equipped,
            permissions=permissions,
            tool_call_timeout_s=tool_call_timeout_s,
            metrics=metrics,
            scope_provider=scope_provider,
            runtime_id=runtime_id,
        ),
    )
    graph.add_node(_LIMIT_REACHED_NODE, partial(_limit_reached, metrics=metrics))
    graph.add_node(
        _TOOL_ERRORS_NODE,
        partial(_tool_errors_reached, metrics=metrics, reply=tool_error_reply),
    )

    graph.set_entry_point("call_model")
    graph.add_conditional_edges(
        "call_model", partial(_route, max_tool_calls=max_tool_calls)
    )
    graph.add_conditional_edges(
        "execute_tools", partial(_after_tools, max_tool_errors=max_tool_errors)
    )
    graph.add_edge(_LIMIT_REACHED_NODE, END)
    graph.add_edge(_TOOL_ERRORS_NODE, END)

    return graph
