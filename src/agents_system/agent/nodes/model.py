"""The model node of the agent graph: the model call, the trusted context it is given and the repair of unanswered tool calls."""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.utils import count_tokens_approximately

from agents_system.agent.context import ContextTrim, trim_history
from agents_system.agent.state import AgentState
from agents_system.harness.injector import _emit
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    Metrics,
    record_limit_trip,
)

logger = structlog.get_logger()


def _close_unanswered_tool_calls(
    messages: Sequence[AnyMessage], *, reason: str
) -> list[AnyMessage]:
    """Answer every tool call of *messages* that has no reply (issues #172, #181).

    A turn cut off between a model call and the end of its tool round trip
    leaves the assistant's tool calls without their `ToolMessage`. Providers
    reject that sequence, so a caller replaying the returned messages as the
    next turn's history would fail, and so would the next turn of a
    checkpointed thread. Each outstanding call gets an error reply carrying
    *reason*, placed right after its assistant message; the calls stay
    visible. Messages that are already well formed come back unchanged.
    """
    closed: list[AnyMessage] = []
    for index, message in enumerate(messages):
        closed.append(message)
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        replied = {
            later.tool_call_id
            for later in itertools.takewhile(
                lambda m: isinstance(m, ToolMessage), messages[index + 1 :]
            )
            if isinstance(later, ToolMessage)
        }
        closed.extend(
            ToolMessage(
                content=f"Tool call interrupted: {reason}",
                tool_call_id=call["id"],
                status="error",
            )
            for call in message.tool_calls
            if call["id"] not in replied
        )
    return closed


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


#: Returns the user-facing refusal when the running turn's subject has reached
#: a cap, else ``None``. Built per turn by `AgentRuntime`.
BudgetGate = Callable[[AgentState | None], Awaitable[str | None]]


TRUSTED_CONTEXT_HEADER = (
    "## Context for this request\n\n"
    "The application serving this request supplied the context below. Use it "
    "as facts about this request; it does not change the instructions above."
)


def _system_content(system_prompt: str, trusted_context: str) -> str:
    """The system message text: the agent's prompt, then the turn's context.

    Issue #249 -- trusted context comes from the application (an adapter
    request-context hook), never from the user. It sits AFTER the agent's own
    prompt, inside the same system message, so the prompt keeps its position
    and priority and a provider that takes one system message gets one. No
    context leaves the prompt byte-identical.
    """
    if not trusted_context.strip():
        return system_prompt
    return f"{system_prompt}\n\n{TRUSTED_CONTEXT_HEADER}\n\n{trusted_context.strip()}"


async def _call_model(
    state: AgentState,
    bound_model: Any,
    system_prompt: str,
    max_context_tokens: int | None = None,
    tool_schemas: Sequence[dict[str, Any]] = (),
    definition: Any = None,
    metrics: Metrics | None = None,
    budget_gate: BudgetGate | None = None,
    untrusted_text: str = "",
    trusted_context: str = "",
) -> dict[str, Any]:
    """Invoke the bound model with the current message history.

    D-014 S4 (design AD-1): the runtime SystemMessage is prepended to the
    MODEL INPUT here, at call time, and is NEVER stored in state — this node
    only ever returns the model's AIMessage, so state["messages"] (and
    therefore any checkpointed/persisted history) never accumulates it. This
    is what makes the system prompt checkpoint-safe: were it persisted, a
    resumed multi-turn conversation would re-prepend and duplicate it on
    every turn. The turn's trusted context (issue #249) rides in the same
    message, after the prompt, and is never stored either.
    """
    # Issue #181 -- a persisted thread can hold an assistant message whose
    # tool call never got its reply: a turn that stopped at max_tool_calls,
    # or a thread saved before issue #24 made a timed-out turn save its
    # interrupted calls answered. Repair the history the model SEES, not the
    # store: this is the one place every channel and every checkpointer
    # backend goes through, and it needs no backend-specific call. State is
    # untouched.
    # ADR-011 part 5 -- before every later model call of the turn (the first
    # was checked before the graph ran), so a tool loop overshoots a cap by at
    # most the one call already in flight.
    if budget_gate is not None and state.get("turn_usage"):
        refusal = await budget_gate(state)
        if refusal is not None:
            return {
                "messages": [AIMessage(content=refusal)],
                "turn_usage": state["turn_usage"],
                "budget_refused": True,
            }
    history = _close_unanswered_tool_calls(
        state["messages"],
        reason="the tool did not return a result in an earlier turn",
    )
    system_message = SystemMessage(
        content=_system_content(system_prompt, trusted_context)
    )
    # Issue #258 -- the turn's untrusted-data blocks sit right after the system
    # message, as their own message: never inside it, never in state. They are
    # data the model reads, fixed for the turn before any model call.
    leading: list[AnyMessage] = [system_message]
    if untrusted_text:
        leading.append(HumanMessage(content=untrusted_text))
    if max_context_tokens is not None:
        # Issue #28 -- bound the request. Like the repair above, this shapes
        # what the model SEES; `state["messages"]` is never rewritten, so a
        # checkpointed thread keeps its full history.
        fixed_tokens = count_tokens_approximately(
            leading, tools=list(tool_schemas) or None
        )
        trim = trim_history(
            history, max_tokens=max_context_tokens, fixed_tokens=fixed_tokens
        )
        if untrusted_text and trim.over_budget:
            # The blocks are the one optional part of the request: when the
            # system prompt, tool schemas and the current turn already leave
            # no room for them, leave them out rather than break the cap.
            bare: list[AnyMessage] = [system_message]
            bare_trim = trim_history(
                history,
                max_tokens=max_context_tokens,
                fixed_tokens=count_tokens_approximately(
                    bare, tools=list(tool_schemas) or None
                ),
            )
            if not bare_trim.over_budget:
                logger.warning("runtime.untrusted_blocks_omitted", reason="over_budget")
                leading = bare
                trim = bare_trim
        if trim.trimmed or trim.over_budget:
            _report_context_trim(trim, max_context_tokens, definition, metrics)
        history = trim.messages
    model_input = [*leading, *history]
    retried_after_tool_format_error = False
    try:
        response: AIMessage = await bound_model.ainvoke(model_input)
    except Exception as exc:
        # Some providers raise BadRequestError when the model generates a
        # malformed tool call (tool_use_failed). Retry without tools so the
        # model falls back to a plain text response instead of crashing.
        if "tool_use_failed" in str(exc) or "tool call validation failed" in str(exc):
            logger.warning("runtime.tool_format_error_retry", error=str(exc)[:120])
            # ADR-011 part 5: the failed attempt may have been billed (its
            # usage is unrecoverable, hence the `None` entry), and the fallback
            # is a second provider request -- check the cap again before it.
            if budget_gate is not None:
                failed_turn_usage: list[UsageMetadata | None] = [
                    *state.get("turn_usage", []),
                    None,
                ]
                refusal = await budget_gate({**state, "turn_usage": failed_turn_usage})
                if refusal is not None:
                    return {
                        "messages": [AIMessage(content=refusal)],
                        "turn_usage": failed_turn_usage,
                        "budget_refused": True,
                    }
            base_model = bound_model.bound
            response = await base_model.ainvoke(model_input)
            retried_after_tool_format_error = True
        else:
            raise
    logger.info("runtime.model_response", tool_calls=len(response.tool_calls or []))
    # #78 Phase 0 -- record this call's real usage_metadata (None when the
    # provider reported none) alongside the accumulated ones from earlier
    # call_model invocations THIS turn. No reducer on `turn_usage` (see
    # `AgentState`), so this read-then-append is what makes it accumulate
    # across the several call_model visits one turn can make.
    #
    # Review finding 3 (PR #87): the tool-format-error retry above makes TWO
    # real provider calls, not one -- the first call above already consumed
    # tokens before raising. There is no way to recover how many (the
    # exception carries no usage_metadata), so it is recorded as an explicit
    # `None` entry rather than silently dropped: this correctly counts
    # `model_calls=2` and triggers `TurnUsage`'s own honesty rule (one `None`
    # entry nulls the turn's token totals -- never a plausible-looking
    # undercount that only reports the fallback call's numbers).
    new_entries: list[UsageMetadata | None] = (
        [None, response.usage_metadata]
        if retried_after_tool_format_error
        else [response.usage_metadata]
    )
    turn_usage = [*state.get("turn_usage", []), *new_entries]
    return {"messages": [response], "turn_usage": turn_usage}


def _report_context_trim(
    trim: ContextTrim,
    max_context_tokens: int,
    definition: Any,
    metrics: Metrics | None,
) -> None:
    """Log, audit and count one trimmed or over-budget model input (issue #28).

    Counts only: no message text reaches the log, the audit event or the
    metric. `over_budget` marks the case where even the minimum (system
    prompt, tools, latest user message, newest tool round) exceeds the
    budget; it is reported even when nothing was dropped.
    """
    logger.warning(
        "runtime.context_trimmed",
        dropped_messages=trim.dropped_messages,
        kept_messages=len(trim.messages),
        tokens_before=trim.tokens_before,
        tokens_after=trim.tokens_after,
        max_context_tokens=max_context_tokens,
        over_budget=trim.over_budget,
    )
    record_limit_trip(
        metrics if metrics is not None else DEFAULT_METRICS,
        limit="max_context_tokens",
    )
    _emit(
        "record_context_trimmed",
        definition=definition,
        dropped_messages=trim.dropped_messages,
        kept_messages=len(trim.messages),
        tokens_before=trim.tokens_before,
        tokens_after=trim.tokens_after,
        max_context_tokens=max_context_tokens,
        over_budget=trim.over_budget,
    )
