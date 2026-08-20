"""AgentRuntime — LangGraph-based, provider-agnostic agent loop (D-007/D-009).

Turns a static EquippedRuntime into a live multi-turn agent via a 2-node
LangGraph graph:
  - call_model  : invokes the bound model with the current message history
  - execute_tools: dispatches each tool_call through the Layer-2 interceptor

The loop continues until the model produces an AIMessage with no tool_calls.

D-009: intercept() is now async-native. Sync connectors are wrapped with
asyncio.to_thread inside intercept() itself. _execute_tools opens one
turn-scoped AsyncSession (when session_provider is set) and forwards it to
intercept(). The session is NOT committed here — the orchestrator/webhook owns
commit after run_turn() returns.

Concurrency note: the tool-call loop remains sequential. A shared AsyncSession
must not be used concurrently — do not convert the loop to asyncio.gather.

State is NOT persisted internally. The caller supplies the full message history
per turn and owns cross-turn durability.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from functools import partial
from typing import Any, Mapping

import redis.exceptions
import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from agentsys.agent.state import AgentState
from agentsys.harness.factory import EquippedRuntime
from agentsys.harness.injector import _emit
from agentsys.harness.interceptor import CallResult, PolicyViolation, intercept
from agentsys.harness.loader import PLATFORM_DEFAULT_LIMITS

logger = structlog.get_logger()

# Terminal node reached when a turn exhausts its max_tool_calls budget.
_LIMIT_REACHED_NODE = "limit_reached"

# The subset of PLATFORM_DEFAULT_LIMITS the agent loop enforces (it also
# carries max_delegation_depth/max_clarification_attempts, not yet read here).
_ENFORCED_LIMIT_KEYS = (
    "max_tool_calls",
    "total_execution_timeout_s",
    "tool_call_timeout_s",
)


def _effective_limits(execution_limits: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge a runtime's execution_limits over the platform defaults, per-key.

    Design AD-3: a partially-specified execution_limits dict (e.g. only
    ``max_tool_calls`` overridden) falls back to the platform default for
    every key it does not itself set.
    """
    merged: dict[str, Any] = {
        key: PLATFORM_DEFAULT_LIMITS[key] for key in _ENFORCED_LIMIT_KEYS
    }
    if execution_limits:
        for key in _ENFORCED_LIMIT_KEYS:
            value = execution_limits.get(key)
            if value is not None:
                merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def _call_model(
    state: AgentState, bound_model: Any, system_prompt: str
) -> dict[str, Any]:
    """Invoke the bound model with the current message history.

    D-014 S4 (design AD-1): the runtime SystemMessage is prepended to the
    MODEL INPUT here, at call time, and is NEVER stored in state — this node
    only ever returns the model's AIMessage, so state["messages"] (and
    therefore any checkpointed/persisted history) never accumulates it. This
    is what makes the system prompt checkpoint-safe: were it persisted, a
    resumed multi-turn conversation would re-prepend and duplicate it on
    every turn.
    """
    model_input = [SystemMessage(content=system_prompt), *state["messages"]]
    try:
        response: AIMessage = await bound_model.ainvoke(model_input)
    except Exception as exc:
        # Some providers raise BadRequestError when the model generates a
        # malformed tool call (tool_use_failed). Retry without tools so the
        # model falls back to a plain text response instead of crashing.
        if "tool_use_failed" in str(exc) or "tool call validation failed" in str(exc):
            logger.warning("runtime.tool_format_error_retry", error=str(exc)[:120])
            base_model = bound_model.bound
            response = await base_model.ainvoke(model_input)
        else:
            raise
    logger.info("runtime.model_response", tool_calls=len(response.tool_calls or []))
    return {"messages": [response]}


async def _execute_tools(
    state: AgentState,
    equipped: EquippedRuntime,
    permissions: tuple[str, ...],
    tool_call_timeout_s: float,
) -> dict[str, Any]:
    """Execute all tool_calls in the last AIMessage through the Layer-2 interceptor.

    D-009: Opens one turn-scoped AsyncSession when equipped.session_provider is
    set; passes the session to every intercept() call in this turn. The session
    is NOT committed — the orchestrator owns the transaction boundary.
    The loop is intentionally sequential: a shared AsyncSession must not be
    used concurrently (no asyncio.gather here).

    D-014 S2 (design AD-3): each intercept() call is bounded by
    tool_call_timeout_s so one slow connector cannot consume the whole turn
    budget — on timeout an error ToolMessage is appended and the loop
    continues (same shape as the PolicyViolation handling below).
    tool_call_count is incremented by the number of calls attempted this node
    execution (not just successful ones), matching max_tool_calls semantics.
    """
    last_message = state["messages"][-1]
    tool_calls: list[dict[str, Any]] = getattr(last_message, "tool_calls", []) or []

    result_messages: list[ToolMessage] = []

    session_cm = equipped.session_provider() if equipped.session_provider else nullcontext()
    async with session_cm as session:
        for call in tool_calls:
            tool_name: str = call["name"]
            tool_args: dict[str, Any] = call.get("args", {}) or {}
            call_id: str = call["id"]

            try:
                async with asyncio.timeout(tool_call_timeout_s):
                    outcome: CallResult = await intercept(
                        tool_name,
                        tool_args,
                        equipped,
                        current_permissions=permissions,
                        session=session,
                    )
                output = outcome.output
                content = (
                    json.dumps(output) if isinstance(output, (dict, list)) else str(output)
                )
                result_messages.append(
                    ToolMessage(content=content, tool_call_id=call_id)
                )
                logger.info("runtime.tool_executed", tool=tool_name)
            except TimeoutError:
                result_messages.append(
                    ToolMessage(
                        content=f"Tool call timed out after {tool_call_timeout_s}s",
                        tool_call_id=call_id,
                        status="error",
                    )
                )
                logger.warning(
                    "runtime.tool_call_timeout",
                    tool=tool_name,
                    timeout_s=tool_call_timeout_s,
                )
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

    return {
        "messages": result_messages,
        "tool_call_count": state.get("tool_call_count", 0) + len(tool_calls),
    }


async def _limit_reached(state: AgentState) -> dict[str, Any]:
    """Terminal node reached when tool_call_count exhausts max_tool_calls.

    Design AD-3: appends a non-empty terminal AIMessage (never an empty/silent
    reply) and logs the breach with the count that triggered it.
    """
    tool_call_count = state.get("tool_call_count", 0)
    logger.warning("runtime.limit_reached", tool_call_count=tool_call_count)
    return {
        "messages": [
            AIMessage(
                content=(
                    "I could not complete this within the allowed number of "
                    "steps. Please rephrase or try again."
                )
            )
        ]
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


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
) -> StateGraph[AgentState]:
    """Build the StateGraph with call_model, execute_tools and limit_reached nodes."""
    graph = StateGraph(AgentState)

    graph.add_node(
        "call_model",
        partial(
            _call_model, bound_model=bound_model, system_prompt=equipped.system_prompt
        ),
    )
    graph.add_node(
        "execute_tools",
        partial(
            _execute_tools,
            equipped=equipped,
            permissions=permissions,
            tool_call_timeout_s=tool_call_timeout_s,
        ),
    )
    graph.add_node(_LIMIT_REACHED_NODE, _limit_reached)

    graph.set_entry_point("call_model")
    graph.add_conditional_edges(
        "call_model", partial(_route, max_tool_calls=max_tool_calls)
    )
    graph.add_edge("execute_tools", "call_model")
    graph.add_edge(_LIMIT_REACHED_NODE, END)

    return graph


# ---------------------------------------------------------------------------
# Checkpointer opt-in invocation (design AD-1, AD-8)
# ---------------------------------------------------------------------------


async def _ainvoke_with_optional_checkpointer(
    graph: StateGraph[AgentState],
    checkpointer: BaseCheckpointSaver[Any] | None,
    thread_id: str | None,
    initial_state: AgentState,
    recursion_limit: int,
) -> dict[str, Any]:
    """Invoke the compiled graph, honoring the AD-1 opt-in checkpointer and
    the AD-8 same-turn checkpointer-degradation fallback.

    - ``thread_id`` is ``None`` (adapter path, or
      ``whatsapp_checkpointer_enabled=False``): compiles and invokes WITHOUT
      a checkpointer — identical to the pre-D-014 stateless behavior.
    - ``thread_id`` is given AND a ``checkpointer`` is configured: compiles
      WITH the checkpointer and engages it via
      ``config["configurable"]["thread_id"]``. If that invoke raises a
      checkpointer BACKEND failure (``redis.RedisError`` — covers
      ``ConnectionError``/the redis client's own ``TimeoutError``, which does
      NOT subclass the builtin ``TimeoutError``), the failure is logged as
      ``runtime.checkpointer_degraded`` and the SAME turn is retried,
      compiled WITHOUT a checkpointer, over the original ``initial_state``
      (i.e. only the caller-supplied messages for this turn — no persisted
      history for this one turn; see design AD-8).

    ``asyncio.TimeoutError``/the builtin ``TimeoutError`` from the turn-scope
    budget (AD-3, enforced by the caller wrapping this call in
    ``asyncio.timeout``) is a DISTINCT failure class and is never caught
    here — it propagates to run_turn's own timeout handler. ``CancelledError``
    is never caught either.
    """
    if thread_id is not None and checkpointer is not None:
        compiled_with_checkpointer = graph.compile(checkpointer=checkpointer)
        checkpointed_config: RunnableConfig = {
            "recursion_limit": recursion_limit,
            "configurable": {"thread_id": thread_id},
        }
        try:
            return dict(
                await compiled_with_checkpointer.ainvoke(
                    initial_state, config=checkpointed_config
                )
            )
        except redis.exceptions.RedisError as exc:
            logger.warning(
                "runtime.checkpointer_degraded",
                thread_id=thread_id,
                error=str(exc),
            )

    compiled = graph.compile()
    return dict(
        await compiled.ainvoke(
            initial_state, config={"recursion_limit": recursion_limit}
        )
    )


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
    checkpointer:
        Optional shared LangGraph checkpointer (design AD-1). Only engaged
        when a ``thread_id`` is also passed to a given ``run_turn`` call —
        entry points with their own full-history contract (e.g. the OpenAI
        adapter) never pass a ``thread_id`` and stay byte-identical to the
        pre-D-014 stateless behavior.
    """

    def __init__(
        self,
        runtime: EquippedRuntime,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self._runtime = runtime
        self._equipped = runtime
        self._checkpointer = checkpointer
        self._schemas = [spec.to_langchain_tool_schema() for spec in runtime.tools]
        # Only call bind_tools when there are tools to bind — some fake models
        # raise NotImplementedError for bind_tools even with an empty list.
        self._bound_model = model.bind_tools(self._schemas) if self._schemas else model
        logger.info(
            "runtime.initialized",
            tools=len(self._schemas),
            model_type=type(model).__name__,
        )
        # D-007: record runtime_initialized event
        _emit(
            "record_runtime_initialized",
            definition=self._equipped.definition,
            tools_count=len(self._schemas),
            model_type=type(model).__name__,
        )

    @property
    def permissions(self) -> tuple[str, ...]:
        """The runtime's own resolved permission grants (design AD-4)."""
        return self._equipped.definition.permissions

    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
    ) -> list[AnyMessage]:
        """Execute one conversational turn.

        By default the runtime is stateless: it does NOT persist messages
        between calls, and the caller supplies the full message history each
        turn. Passing ``thread_id`` opts THIS call into the shared checkpointer
        (design AD-1) — cross-turn history then accumulates server-side,
        keyed by ``thread_id``, and ``messages`` only needs to carry the new
        turn's message(s).

        Parameters
        ----------
        messages:
            Message(s) for this turn. Full history when stateless
            (``thread_id=None``); only the new turn's message(s) when a
            checkpointer is engaged (``thread_id`` given).
        session_id:
            Logical session identifier (passed through, stored in AgentState).
        permissions:
            The caller's current permission grants used by the Layer-2 interceptor
            to validate sensitive tool calls at execution time. Defaults to
            ``None``, in which case the runtime's own resolved grants
            (``self.permissions``) are used (design AD-4) — this is the
            correct default for every entry point that has no separate
            identity of its own (OpenAI adapter, WhatsApp webhook).
        thread_id:
            Opt-in checkpointer key (design AD-1), e.g. the client's
            normalized phone number. ``None`` (default) keeps this call fully
            stateless — no checkpointer is engaged even if one is configured
            on this runtime. Ignored (treated as stateless) if this runtime
            was constructed without a ``checkpointer``.

        Returns
        -------
        list[AnyMessage]
            All messages accumulated during this turn (input + model responses +
            tool messages). When stateless, the caller owns cross-turn
            aggregation; when a checkpointer is engaged, the checkpointer owns
            cross-turn accumulation and this return value already reflects it.
        """
        effective_permissions = (
            permissions if permissions is not None else self.permissions
        )
        effective_limits = _effective_limits(self._equipped.definition.execution_limits)
        max_tool_calls = effective_limits["max_tool_calls"]
        graph = _build_graph(
            self._equipped,
            self._bound_model,
            effective_permissions,
            max_tool_calls=max_tool_calls,
            tool_call_timeout_s=effective_limits["tool_call_timeout_s"],
        )
        # D-014 S4 (design AD-1): the system prompt is no longer prepended
        # here — _call_model prepends it to the MODEL INPUT on every call and
        # never persists it in state (see _call_model docstring).
        all_messages = list(messages)
        initial_state: AgentState = {
            "messages": all_messages,
            "session_id": session_id,
            "current_permissions": effective_permissions,
            # Always set explicitly (not inherited from a prior checkpoint):
            # a per-turn budget must reset to 0 even when this thread resumes
            # from persisted state (design AD-1 gotcha).
            "tool_call_count": 0,
        }
        # D-014 S2 (design AD-3): a hard recursion_limit backstop derived from
        # max_tool_calls. LangGraph's own default (25 super-steps) is too low
        # for a legitimately full-budget turn (roughly 2 steps per tool call
        # plus the final call_model step) — without this override a turn that
        # stays exactly within its own max_tool_calls budget could still crash
        # with GraphRecursionError instead of completing normally.
        recursion_limit = 2 * max_tool_calls + 10
        try:
            async with asyncio.timeout(effective_limits["total_execution_timeout_s"]):
                result = await _ainvoke_with_optional_checkpointer(
                    graph,
                    self._checkpointer,
                    thread_id,
                    initial_state,
                    recursion_limit,
                )
        except TimeoutError:
            logger.warning(
                "runtime.timeout",
                total_execution_timeout_s=effective_limits["total_execution_timeout_s"],
            )
            # D-007: record runtime_timeout event
            _emit(
                "record_runtime_timeout",
                definition=self._equipped.definition,
                total_execution_timeout_s=effective_limits["total_execution_timeout_s"],
            )
            return all_messages + [
                AIMessage(
                    content=(
                        "This is taking longer than expected. Please try again."
                    )
                )
            ]
        return list(result["messages"])
