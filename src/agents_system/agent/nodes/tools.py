"""The tool nodes of the agent graph: executing tool calls and the two terminal nodes a tripped limit ends in."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from typing import Any

import structlog
from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)

from agents_system.agent.state import AgentState
from agents_system.agent.tool_errors import aborting_exceptions, tool_error_text
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.interceptor import CallResult, PolicyViolation, intercept
from agents_system.observability.metrics import (
    Metrics,
    record_limit_trip,
    record_tool_call,
)
from agents_system.scope import ScopeProvider

logger = structlog.get_logger()


#: The fixed `tool` label value recorded for a "denied"/not_in_surface tool
#: call, in place of the model-supplied `tool_name` -- see the note in
#: `_execute_tools`'s `PolicyViolation` handling below.
_UNRECOGNIZED_TOOL_LABEL = "_unrecognized_"


async def _execute_tools(
    state: AgentState,
    equipped: EquippedRuntime,
    permissions: tuple[str, ...],
    tool_call_timeout_s: float,
    metrics: Metrics,
    scope_provider: ScopeProvider | None = None,
    runtime_id: str | None = None,
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

    #78 Phase 0 Slice 2 -- every branch below records one `record_tool_call`
    at this single choke point (never scattered elsewhere): "ok" on a
    successful connector call, "denied" for a `PolicyViolation` on a tool
    that was never in the equipped surface at all (reason="not_in_surface"),
    "blocked" for a Layer-2 revalidation failure on a tool that WAS equipped
    (reason in {"revalidation_required", "permission_revoked"}), "timeout"
    when the call exceeds tool_call_timeout_s, and "error" for anything else
    the connector itself raises.

    #289 -- an ``Exception`` a tool raises is recorded as "error" and then
    handed to the model as an error ToolMessage (``status="error"``, see
    ``agent/tool_errors.py``) so it can correct itself. The exceptions that
    carry a security or permission decision (``aborting_exceptions()``) are
    recorded and re-raised, aborting the turn, and ``BaseException``
    (cancellation, the turn's own timeout) is never caught. Every error
    ToolMessage from a raise or a timeout counts in ``tool_error_count``
    (``max_tool_errors``).

    The "denied" branch records the fixed `_UNRECOGNIZED_TOOL_LABEL`
    instead of the model-supplied `tool_name` -- that name was never checked
    against the equipped surface, so it is untrusted (possibly
    prompt-injected or hallucinated) text with no monitoring value, and must
    never reach the `tool` label (label hygiene, same rule as `session_id`).
    """
    last_message = state["messages"][-1]
    tool_calls: list[dict[str, Any]] = getattr(last_message, "tool_calls", []) or []

    result_messages: list[ToolMessage] = []
    tool_errors = 0

    session_cm = (
        equipped.session_provider() if equipped.session_provider else nullcontext()
    )
    async with session_cm as session:
        for call in tool_calls:
            tool_name: str = call["name"]
            tool_args: dict[str, Any] = call.get("args", {}) or {}
            call_id: str = call["id"]
            call_start = time.monotonic()

            try:
                async with asyncio.timeout(tool_call_timeout_s):
                    call_result: CallResult = await intercept(
                        tool_name,
                        tool_args,
                        equipped,
                        current_permissions=permissions,
                        session=session,
                        scope_provider=scope_provider,
                        runtime_id=runtime_id,
                    )
                output = call_result.output
                content = (
                    json.dumps(output)
                    if isinstance(output, (dict, list))
                    else str(output)
                )
                result_messages.append(
                    ToolMessage(content=content, tool_call_id=call_id)
                )
                logger.info("runtime.tool_executed", tool=tool_name)
                record_tool_call(
                    metrics,
                    tool=tool_name,
                    outcome="ok",
                    duration_s=time.monotonic() - call_start,
                )
            except TimeoutError:
                tool_errors += 1
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
                record_tool_call(
                    metrics,
                    tool=tool_name,
                    outcome="timeout",
                    duration_s=time.monotonic() - call_start,
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
                not_in_surface = violation.reason == "not_in_surface"
                policy_outcome = "denied" if not_in_surface else "blocked"
                # "denied"/not_in_surface means `tool_name` was never checked
                # against the equipped surface -- by definition it is not a
                # real registered connector name, so it is untrusted, model-
                # supplied text (a prompt-injected or hallucinated "tool"
                # call) with zero monitoring value. Recording it verbatim as
                # a Prometheus label would let that text -- including PII --
                # reach the process-wide, persistent /metrics label store
                # (see docs/platform/observability.md's "Label hygiene").
                # Every OTHER outcome's tool_name is already bound to a real,
                # equipped ToolSpec and stays as is.
                metric_tool_name = (
                    _UNRECOGNIZED_TOOL_LABEL if not_in_surface else tool_name
                )
                record_tool_call(
                    metrics,
                    tool=metric_tool_name,
                    outcome=policy_outcome,
                    duration_s=time.monotonic() - call_start,
                )
            except Exception as error:
                record_tool_call(
                    metrics,
                    tool=tool_name,
                    outcome="error",
                    duration_s=time.monotonic() - call_start,
                )
                if isinstance(error, aborting_exceptions()):
                    # A security or permission decision: the turn aborts, the
                    # model is not told to try around it (agent/tool_errors.py).
                    raise
                tool_errors += 1
                logger.warning(
                    "runtime.tool_error",
                    tool=tool_name,
                    error_class=type(error).__name__,
                )
                result_messages.append(
                    ToolMessage(
                        content=tool_error_text(tool_name, error),
                        tool_call_id=call_id,
                        status="error",
                    )
                )

    return {
        "messages": result_messages,
        "tool_call_count": state.get("tool_call_count", 0) + len(tool_calls),
        "tool_error_count": state.get("tool_error_count", 0) + tool_errors,
    }


async def _limit_reached(state: AgentState, metrics: Metrics) -> dict[str, Any]:
    """Terminal node reached when tool_call_count exhausts max_tool_calls.

    Design AD-3: appends a non-empty terminal AIMessage (never an empty/silent
    reply) and logs the breach with the count that triggered it.

    #78 Phase 0 Slice 2 -- the only limit this loop enforces via a terminal
    node today, so `limit="max_tool_calls"` is not yet a caller-supplied
    value; see `ADR-005` section 6.
    """
    tool_call_count = state.get("tool_call_count", 0)
    logger.warning("runtime.limit_reached", tool_call_count=tool_call_count)
    record_limit_trip(metrics, limit="max_tool_calls")
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


async def _tool_errors_reached(
    state: AgentState, metrics: Metrics, reply: str
) -> dict[str, Any]:
    """Terminal node reached when a turn's tool errors reach max_tool_errors (#289).

    Ends the turn with the deployment's controlled reply, sent as given and
    never templated with anything from the turn, instead of letting the model
    retry a failing tool again.
    """
    logger.warning(
        "runtime.tool_errors_reached",
        tool_error_count=state.get("tool_error_count", 0),
    )
    record_limit_trip(metrics, limit="max_tool_errors")
    return {"messages": [AIMessage(content=reply)]}
