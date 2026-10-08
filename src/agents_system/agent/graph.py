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

State is NOT persisted by the graph itself. A stateless caller supplies the
full message history per turn. A caller that passes a ``thread_id`` to a runtime
built with a checkpointer gets the thread's history read before the turn and
the turn saved after it (issue #24, see ``_Thread``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, nullcontext
from functools import partial
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from agents_system.agent.context import ContextTrim, trim_history
from agents_system.agent.models import build_agent_model, declared_model
from agents_system.agent.state import AgentState
from agents_system.agent.untrusted import (
    DEFAULT_TURN_MAX_CHARS,
    UntrustedData,
    render_untrusted_blocks,
)
from agents_system.config import ModelPrice
from agents_system.execution_limits import (
    DEFAULT_TIMEOUT_REPLY,
    DEPLOYMENT_LIMIT_KEYS,
    check_effective_limits,
    validate_execution_limits,
    validate_timeout_reply,
)
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.injector import _emit
from agents_system.harness.interceptor import CallResult, PolicyViolation, intercept
from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS
from agents_system.memory_policy import allows_user_memory, persists_conversation
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    Metrics,
    record_checkpointer_degraded,
    record_limit_trip,
    record_tool_call,
    record_turn,
)
from agents_system.permissions import UnknownPermissionNameError, covers
from agents_system.permissions.permission_registry import permission_registry
from agents_system.scope import ScopeProvider
from agents_system.subject import (
    TurnActor,
    TurnSubject,
    current_actor,
    current_subject,
    turn_scope,
)
from agents_system.usage import (
    BudgetCaps,
    UsageLedger,
    UsageRecord,
    UsageTotals,
    reached_cap,
)
from agents_system.user_memory import UserFacts, UserMemoryLimits, backend_for

logger = structlog.get_logger()


# Terminal node reached when a turn exhausts its max_tool_calls budget.
_LIMIT_REACHED_NODE = "limit_reached"

# The subset of PLATFORM_DEFAULT_LIMITS the agent loop enforces (it also
# carries max_delegation_depth/max_clarification_attempts, not yet read here).
_ENFORCED_LIMIT_KEYS = (
    "max_tool_calls",
    "total_execution_timeout_s",
    "tool_call_timeout_s",
    "max_context_tokens",
)


# ---------------------------------------------------------------------------
# #78 Phase 0 — real per-turn token usage and cost
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TurnUsage:
    """Real per-turn token usage and cost -- never an invented number.

    Every field reflects only what the provider actually reported through
    `AIMessage.usage_metadata`, summed across every `call_model` invocation
    this turn made (a turn can make several when the model uses tools).

    - `model_calls` is always the number of provider calls this turn made.
      A checkpointer failure never re-runs a turn (issue #24), so no call
      goes uncounted.
    - A turn that TIMED OUT reports what it accumulated before the budget
      ran out (issue #172): the model calls that completed, and their
      tokens. A request still in flight when the budget expired never
      reported usage and is not counted, so a timed-out turn's totals are a
      lower bound, not an exact figure -- `TurnResult.timed_out` says so.
    - `input_tokens`/`output_tokens`/`total_tokens` are `None` whenever ANY
      of this turn's model calls reported no `usage_metadata`: a partial sum
      is never reported as if it were the whole turn's total.
    - `cost_usd` is `None` whenever any token total above is `None`, no
      `model_id` was given, or `model_id` has no entry in the runtime's
      `model_prices` (issue #166: an explicit `AgentRuntime` argument, which
      `create_app` fills from `MODEL_PRICES`) -- a missing price is never
      guessed at.
    """

    model_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None = None


@dataclasses.dataclass(frozen=True)
class TurnResult:
    """The result of one `AgentRuntime.run_turn_with_usage` call: the
    accumulated message list plus this turn's real `TurnUsage` (issue #78,
    review finding 5).

    A dedicated, explicit type -- NOT a `list[AnyMessage]` subclass. An
    earlier design (`TurnMessages`) subclassed `list` to carry `.usage` as
    an attribute for backward compatibility, but ordinary list operations
    (slicing, `+`, `list(...)`, unpacking, LangGraph's own `add_messages`
    reducer) all construct a plain `list` under the hood, which silently
    dropped `.usage` -- a caller that transformed the result before reading
    `.usage` lost it with no error. `run_turn` keeps returning a real
    `list[AnyMessage]` (every existing caller that only needs the message
    list -- `evals/runner.py`, `services/webhook_worker.py`, the wider test
    suite -- keeps working unmodified); `run_turn_with_usage` is the
    explicit entry point for a caller that also wants `.usage`, and it is
    always this exact type, never silently degraded to a plain list.
    """

    messages: list[AnyMessage]
    usage: TurnUsage
    #: Issue #172 -- the turn hit ``total_execution_timeout_s``. `messages`
    #: and `usage` then hold what accumulated before the budget ran out,
    #: followed by the sendable timeout reply, so a caller can tell a
    #: timeout from an answer that merely looks truncated.
    timed_out: bool = False
    #: The turn budget (seconds) that expired; ``None`` unless `timed_out`.
    timeout_s: float | None = None
    #: ADR-011 part 5 -- a per-user cap refused this turn (before the graph
    #: ran) or stopped it at a later model call. `messages` then ends with the
    #: configured user-facing message; `usage` holds what was spent before.
    budget_refused: bool = False


@dataclasses.dataclass
class _TurnProgress:
    """The latest graph state a turn reached, kept outside the invocation so
    a timeout (which cancels it and returns nothing) can still read back
    the tool calls, model calls and usage accumulated so far (issue #172)."""

    state: dict[str, Any] | None = None


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


_UNKNOWN_TURN_USAGE = TurnUsage(
    model_calls=None, input_tokens=None, output_tokens=None, total_tokens=None
)


def model_display_name(model: BaseChatModel) -> str:
    """Best-effort human-readable model identifier across every
    `_build_chat_model` provider branch (issue #78 review finding 4).

    The ChatOpenAI-family classes (`groq`, `openai_compatible` -- including
    OpenRouter, which reports ids like `"deepseek/deepseek-v4-flash"`)
    expose the configured model as `model_name`; `ChatOllama`/`ChatAnthropic`
    expose it as `model`. Falls back to the class name so a result is always
    reportable even for a provider shape not seen before -- never raises.

    This is `AgentRuntime`'s own default `model_prices` lookup key
    (see `AgentRuntime.__init__`) -- prices belong to the provider model
    actually billing the tokens, not to whichever runtime id a caller used
    to select this agent. Originally defined in `evals/provider.py`; moved
    here so both the live-eval pipeline and every `AgentRuntime` entry point
    (the OpenAI adapter, the WhatsApp webhook worker) share the exact same
    derivation instead of two copies drifting apart. `evals/provider.py`
    re-exports this name for backward compatibility.
    """
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def _aggregate_turn_usage(entries: Sequence[UsageMetadata | None]) -> TurnUsage:
    """Sum this turn's `call_model` usage entries into one `TurnUsage`.

    Honesty rule: a single `None` entry (one model call reported no
    `usage_metadata`) makes the whole turn's token totals `None` -- summing
    only the known entries would silently under-report the turn's real cost.

    PR #87 review finding 3: each entry's own token counts are checked
    against `_is_plausible_token_count` (negative, or at/above
    `_MAX_PLAUSIBLE_TURN_TOKENS`) BEFORE they are summed, not only on the
    resulting aggregate (`_compute_turn_cost`'s own bound check). Bounding
    only the aggregate lets one implausibly huge call and a compensating
    negative call on the same turn net out to a small, "plausible"-looking
    total that would otherwise slip past that later check.
    """
    model_calls = len(entries)
    known: list[UsageMetadata] = [entry for entry in entries if entry is not None]
    if model_calls == 0 or len(known) != model_calls:
        return dataclasses.replace(_UNKNOWN_TURN_USAGE, model_calls=model_calls)
    if any(
        not _is_plausible_token_count(entry["input_tokens"])
        or not _is_plausible_token_count(entry["output_tokens"])
        or not _is_plausible_token_count(entry["total_tokens"])
        for entry in known
    ):
        return dataclasses.replace(_UNKNOWN_TURN_USAGE, model_calls=model_calls)
    return TurnUsage(
        model_calls=model_calls,
        input_tokens=sum(entry["input_tokens"] for entry in known),
        output_tokens=sum(entry["output_tokens"] for entry in known),
        total_tokens=sum(entry["total_tokens"] for entry in known),
    )


# PR #87 review follow-up: the largest per-turn token count any real
# provider response could plausibly report. `usage_metadata` comes straight
# off an `AIMessage` a configured backend returned -- including
# `adapter_provider="openai_compatible"`, an explicitly supported,
# operator-selectable third-party/self-hosted endpoint (MiniMax, vLLM, LM
# Studio, OpenRouter, ...) -- and nothing upstream bounds it: JSON integers
# have no size ceiling, and LangChain's `UsageMetadata` TypedDict does not
# validate or clamp them. 100M tokens is already ~10x the largest known
# context window; a value at or above it, or a negative one, can only come
# from a malformed or hostile response, never a real turn.
_MAX_PLAUSIBLE_TURN_TOKENS = 100_000_000


def _is_plausible_token_count(value: int) -> bool:
    """`True` for a token count a real provider could plausibly report.

    Shared by `_aggregate_turn_usage` (checked per call, before summing) and
    `_compute_turn_cost` (checked again on the resulting aggregate, as
    defense in depth): never negative, never at or above
    `_MAX_PLAUSIBLE_TURN_TOKENS`.
    """
    return 0 <= value <= _MAX_PLAUSIBLE_TURN_TOKENS


def _compute_turn_cost(
    usage: TurnUsage, model_id: str | None, model_prices: Mapping[str, ModelPrice]
) -> float | None:
    """Cost in USD for *usage*, from *model_prices* only.

    Returns `None` -- never a guessed number, and never a crash -- whenever
    `model_id` is unset, the token totals themselves are unknown or outside
    the plausible range a real provider could report (see
    `_MAX_PLAUSIBLE_TURN_TOKENS`), or `model_id` has no entry in
    *model_prices* (design AD note: a price table is opt-in per model id,
    not a global default).

    Issue #166: the table is passed in, never read from the server
    `Settings` -- constructing those fails closed without the server's
    environment, which crashed every in-process library turn at this point,
    after the model and tools had already run.
    """
    if model_id is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    if not _is_plausible_token_count(
        usage.input_tokens
    ) or not _is_plausible_token_count(usage.output_tokens):
        return None
    price = model_prices.get(model_id)
    if price is None:
        return None
    return (
        usage.input_tokens / 1_000_000 * price.input_per_million
        + usage.output_tokens / 1_000_000 * price.output_per_million
    )


def _subject_key() -> str | None:
    subject = current_subject()
    return subject.key if subject is not None else None


def _actor_kind() -> str:
    actor = current_actor()
    return actor.kind if actor is not None else "none"


def _count_tool_messages(messages: Sequence[AnyMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, ToolMessage))


def _actor_label() -> str | None:
    actor = current_actor()
    return actor.label if actor is not None else None


def _effective_limits(
    execution_limits: Mapping[str, Any] | None,
    deployment_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a runtime's execution_limits over the platform defaults, per-key.

    Design AD-3: a partially-specified execution_limits dict (e.g. only
    ``max_tool_calls`` overridden) falls back to the platform default for
    every key it does not itself set.

    #278: a key the deployment sets replaces the platform default and caps
    the definition's own value, so the definition may tighten it, never
    exceed it -- including a platform role that declares above the default.
    A tool-call timeout neither of them sets is capped at the turn's, so a
    turn lowered below the 10 s default does not outlast its own tool call.
    A non-finite declared value raises ``ValueError``: it would bound nothing.
    """
    ceiling = deployment_limits or {}
    declared = execution_limits or {}
    merged: dict[str, Any] = {
        key: ceiling.get(key, PLATFORM_DEFAULT_LIMITS[key])
        for key in _ENFORCED_LIMIT_KEYS
    }
    for key in _ENFORCED_LIMIT_KEYS:
        value = declared.get(key)
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"execution_limits sets {key}={value}, not a finite number"
            )
        merged[key] = min(value, ceiling[key]) if key in ceiling else value
    tool_key = "tool_call_timeout_s"
    if tool_key not in ceiling and declared.get(tool_key) is None:
        merged[tool_key] = min(merged[tool_key], merged["total_execution_timeout_s"])
    return merged


def _tighter(*ceilings: Mapping[str, Any]) -> dict[str, Any]:
    """Per key, the lowest value any of the *ceilings* sets."""
    merged: dict[str, Any] = {}
    for ceiling in ceilings:
        for key, value in ceiling.items():
            merged[key] = min(value, merged[key]) if key in merged else value
    return merged


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


#: Returns the user-facing refusal when the running turn's subject has reached
#: a cap, else ``None``. Built per turn by `AgentRuntime`.
BudgetGate = Callable[[AgentState | None], Awaitable[str | None]]


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
    system_message = SystemMessage(content=system_prompt)
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
    the connector itself raises -- recorded, then re-raised unchanged so
    error handling behavior is exactly what it was before this metric
    existed. The "denied" branch records the fixed `_UNRECOGNIZED_TOOL_LABEL`
    instead of the model-supplied `tool_name` -- that name was never checked
    against the equipped surface, so it is untrusted (possibly
    prompt-injected or hallucinated) text with no monitoring value, and must
    never reach the `tool` label (label hygiene, same rule as `session_id`).
    """
    last_message = state["messages"][-1]
    tool_calls: list[dict[str, Any]] = getattr(last_message, "tool_calls", []) or []

    result_messages: list[ToolMessage] = []

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
            except Exception:
                record_tool_call(
                    metrics,
                    tool=tool_name,
                    outcome="error",
                    duration_s=time.monotonic() - call_start,
                )
                raise

    return {
        "messages": result_messages,
        "tool_call_count": state.get("tool_call_count", 0) + len(tool_calls),
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
    metrics: Metrics,
    max_context_tokens: int | None = None,
    scope_provider: ScopeProvider | None = None,
    runtime_id: str | None = None,
    budget_gate: BudgetGate | None = None,
    untrusted_text: str = "",
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

    graph.set_entry_point("call_model")
    graph.add_conditional_edges(
        "call_model", partial(_route, max_tool_calls=max_tool_calls)
    )
    graph.add_edge("execute_tools", "call_model")
    graph.add_edge(_LIMIT_REACHED_NODE, END)

    return graph


# ---------------------------------------------------------------------------
# Conversation threads and the checkpointer failure policy (issue #24)
# ---------------------------------------------------------------------------

#: How long a thread read or save may take when the caller sets no bound:
#: the same default as the served app's ``CHECKPOINTER_TIMEOUT_S``.
DEFAULT_CHECKPOINTER_TIMEOUT_S = 5.0

#: Checkpointer calls abandoned at their timeout and still being cancelled.
#: Held so the event loop keeps a strong reference until they finish.
_ABANDONED_CALLS: set[asyncio.Future[Any]] = set()


def _forget_abandoned(call: asyncio.Future[Any]) -> None:
    _ABANDONED_CALLS.discard(call)
    if not call.cancelled():
        # Retrieved, so asyncio does not report it as never retrieved; the
        # failure was already handled as a timeout.
        call.exception()


async def _within[T](timeout_s: float, call: Awaitable[T]) -> T:
    """Await *call*, giving up after *timeout_s* seconds with ``TimeoutError``.

    A database that stops answering without refusing anything -- a frozen
    server, a network partition -- raises nothing, and a server-side
    ``statement_timeout`` cannot end the wait either. This bound is on the
    client, so it holds for any saver.

    An abandoned call is cancelled but not awaited: a driver can take a
    while to cancel a request (psycopg waits up to ten seconds for the
    server), and the turn does not wait for that. The cancellation finishes
    in the background, which releases the call's connection.
    """
    task = asyncio.ensure_future(call)
    bound = asyncio.timeout(timeout_s)
    try:
        async with bound:
            return await asyncio.shield(task)
    except TimeoutError:
        if not bound.expired():
            raise  # the call's own error, not this bound
        raise TimeoutError(
            f"the checkpointer did not answer within {timeout_s}s"
        ) from None
    finally:
        if not task.done():
            task.cancel()
            _ABANDONED_CALLS.add(task)
            task.add_done_callback(_forget_abandoned)


@dataclasses.dataclass
class _Thread:
    """One conversation thread in the runtime's checkpointer (issue #24).

    The graph always runs WITHOUT a checkpointer. The thread's history is
    read once before the turn and the turn's new messages are saved once
    after it, so the checkpointer is touched at exactly two points and a
    thread only ever holds whole turns: a process that dies in the middle of
    a turn leaves the thread where the previous turn ended.

    The failure policy keys on which of those two points failed, never on
    the exception class a particular client library raises, so it is the
    same for Postgres, Redis, SQLite or any other LangGraph saver:

    - The read fails (the backend is down, the pool is exhausted, or this
      thread's stored checkpoint cannot be loaded): the turn runs on its own
      messages only, and it is NOT saved -- a thread that could not be read
      is never overwritten by one that lacks its earlier turns.
    - The save fails: the finished answer is returned as it is. Nothing runs
      again, so no model call is repeated and no tool call is replayed; the
      thread stays at its previous turn.

    A backend that never answers -- a frozen or partitioned database server
    raises nothing -- is a failure too: each read and each save is abandoned
    after ``timeout_s`` (``_within``) and handled as the failure of that
    point.

    Both are logged as ``runtime.checkpointer_degraded`` with the
    ``operation`` that failed. A broken thread therefore keeps answering
    without its memory and never affects another thread. ``CancelledError``
    is not an ``Exception`` and is never caught here.
    """

    compiled: Any
    thread_id: str
    timeout_s: float
    metrics: Metrics
    #: What the read returned; ``None`` until a read succeeds.
    history: list[AnyMessage] | None = None

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.thread_id}}

    async def load(self) -> list[AnyMessage]:
        """The thread's history, or ``[]`` when it cannot be read."""
        try:
            snapshot = await _within(
                self.timeout_s, self.compiled.aget_state(self.config)
            )
            history = snapshot.values.get("messages", [])
            # A damaged checkpoint can decode into something that is not a
            # message list at all; that is an unreadable thread too.
            if not isinstance(history, list) or not all(
                isinstance(m, BaseMessage) for m in history
            ):
                raise TypeError("the stored thread is not a list of messages")
        except Exception as exc:
            self._degraded("read", exc)
            return []
        self.history = list(history)
        return self.history

    async def save(self, new_messages: Sequence[AnyMessage]) -> None:
        """Append this turn's messages to the thread, as one checkpoint.

        Saved as if ``call_model`` had written them: the turn always ends
        with a reply that routes to ``END``, so the saved checkpoint has no
        pending step. A save abandoned at its timeout can still complete if
        the database was already writing it; the thread then holds this
        turn, whole, exactly as if the save had answered in time.
        """
        if self.history is None or not new_messages:
            return
        try:
            await _within(
                self.timeout_s,
                self.compiled.aupdate_state(
                    self.config, {"messages": list(new_messages)}, as_node="call_model"
                ),
            )
        except Exception as exc:
            self._degraded("write", exc)

    def _degraded(self, operation: str, exc: Exception) -> None:
        logger.warning(
            "runtime.checkpointer_degraded",
            operation=operation,
            thread_id=self.thread_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # Counted as well: a checkpointer that fails for every turn (a role
        # that cannot save, a database that stopped answering) would
        # otherwise show only as turns that all report "ok".
        record_checkpointer_degraded(self.metrics, operation=operation)


async def _run_tracking_progress(
    compiled: Any,
    initial_state: AgentState,
    config: RunnableConfig,
    tracker: _TurnProgress,
) -> dict[str, Any]:
    """Run *compiled* to completion, recording the state after every step.

    ``astream(stream_mode="values")`` yields the full state after each
    super-step; its last value is exactly what ``ainvoke`` would return. The
    difference is that a cancellation (the turn budget) now leaves the last
    completed step in *tracker* instead of losing everything (issue #172).
    """
    final: dict[str, Any] | None = None
    async with aclosing(
        compiled.astream(initial_state, config=config, stream_mode="values")
    ) as stream:
        async for state in stream:
            final = dict(state)
            tracker.state = final
    if final is None:  # pragma: no cover -- a graph always yields its input
        raise RuntimeError("graph produced no state")
    return final


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: LangChain integration package -> the declarable ``provider`` its chat model
#: classes serve. ``langchain_openai`` covers every OpenAI-protocol host, which
#: is exactly what ``openai_compatible`` declares; the package cannot tell one
#: such host from another, so that is the limit of the comparison.
_PROVIDER_BY_PACKAGE = {
    "langchain_openai": "openai_compatible",
    "langchain_groq": "groq",
    "langchain_anthropic": "anthropic",
    "langchain_ollama": "ollama",
}


def _model_provider(model: BaseChatModel) -> str | None:
    """The declarable provider ``model`` belongs to, or ``None`` if unknown.

    Read from the package that defines the model's class or any of its bases
    (so a subclass such as ``ReasoningSanitizedChatOpenAI`` is still
    recognised). A test double, a custom wrapper or a class from another
    package is unidentifiable: ``None``, never a guess.
    """
    for cls in type(model).__mro__:
        package = cls.__module__.split(".", 1)[0]
        if package in _PROVIDER_BY_PACKAGE:
            return _PROVIDER_BY_PACKAGE[package]
    return None


def _report_model_conflict(definition: Any, model: BaseChatModel) -> None:
    """Warn when the caller's ``model`` is not the one the agent declares.

    Issue #186. The caller's model always wins (see ``AgentRuntime``'s
    ``model`` parameter); this only makes the disagreement visible, so a
    declaration that is not being honored is never silent. Compared by the
    provider model id (`model_display_name`) AND the provider, because two
    providers can serve the same id string. A declared temperature or
    ``max_tokens`` is not comparable on an arbitrary model object, so it is
    not checked. Nothing is built here: the declared model is never
    constructed just to be compared.

    Limits of the comparison: the provider is read from the model's class
    (`_model_provider`), so it separates ``openai_compatible`` / ``groq`` /
    ``anthropic`` / ``ollama`` but not two OpenAI-protocol hosts from each
    other. When the id matches and the provider cannot be identified, the
    models are not called equivalent: ``runtime.model_provider_unverified``
    (info, not a conflict) says the provider could not be established.
    """
    declared = declared_model(definition)
    if declared is None:
        return
    agent = getattr(definition, "role_name", "<unknown>")
    declared_id = declared["model"]
    declared_provider = declared["provider"]
    passed_id = model_display_name(model)
    if passed_id == declared_id:
        passed_provider = _model_provider(model)
        if passed_provider == declared_provider:
            return
        if passed_provider is None:
            logger.info(
                "runtime.model_provider_unverified",
                agent=agent,
                model=declared_id,
                declared_provider=declared_provider,
                detail=(
                    "the passed model reports the declared model id but its "
                    "provider cannot be identified, so it is not known to be "
                    "the declared model; the passed model is used."
                ),
            )
            return
    else:
        passed_provider = _model_provider(model)
    logger.warning(
        "runtime.model_conflict",
        agent=agent,
        declared_model=declared_id,
        passed_model=passed_id,
        declared_provider=declared_provider,
        passed_provider=passed_provider,
        detail=(
            "the agent declares a model but AgentRuntime was given a different "
            "one; the passed model is used and the declaration is ignored. "
            "Drop model= to run on the declared model."
        ),
    )


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

        Issue #186 -- optional. Omitted (``None``), the runtime builds the
        model the agent DECLARES (``Agent(model={...})``, or ``model:`` in its
        frontmatter, inherited through ``extends``) with
        ``agents_system.build_agent_model``, reading credentials only from the
        process environment and ``ProviderSettings`` -- never from the server
        ``Settings``. An agent that declares none raises ``ValueError``.
        Passed AND different from the declared model id, the passed model wins
        and a ``runtime.model_conflict`` warning names both: the caller's
        explicit argument is never silently overridden by configuration, and
        the declaration is never silently inert.
    checkpointer:
        Optional shared LangGraph checkpointer (design AD-1). Only engaged
        when a ``thread_id`` is also passed to a given ``run_turn`` call —
        entry points with their own full-history contract (e.g. the OpenAI
        adapter) never pass a ``thread_id`` and stay byte-identical to the
        pre-D-014 stateless behavior. Any LangGraph saver works; the served
        app passes the Postgres one (issue #24). The thread is read before
        the turn and saved once after it, and a checkpointer that fails
        degrades the turn instead of failing it (see ``_Thread``).
    checkpointer_timeout_s:
        Issue #24 -- the most seconds one thread read or one save may take
        before it is abandoned and handled as a failed read or save, so a
        database that stops answering cannot hold a turn. Enforced on the
        client, for any saver. Defaults to
        ``DEFAULT_CHECKPOINTER_TIMEOUT_S`` (5 seconds); the served app
        passes ``CHECKPOINTER_TIMEOUT_S``. Must be positive.
    runtime_id:
        #78 Phase 0 Slice 2 -- the bounded, low-cardinality ``runtime_id``
        label this runtime's metrics are recorded under (see
        ``observability/metrics.py``'s module docstring on label hygiene).
        ``None`` (default) falls back to this runtime's own derived provider
        model id (``model_display_name(model)``, the same default
        ``run_turn``'s ``model_id`` pricing override already uses) — the
        only caller that has a more meaningful id to give (the registered
        runtime id: the opaque key from ``create_app(agents=...)`` or
        ``AGENT_REGISTRATIONS``) is ``main.py``'s lifespan, which passes it
        explicitly; every other existing construction site
        (``evals/runner.py``, direct library use) is unaffected.
    metrics:
        #78 Phase 0 Slice 2 -- the ``Metrics`` set this runtime's turns,
        tool calls and limit trips are recorded into. ``None`` (default)
        uses the process-wide ``DEFAULT_METRICS`` (the set ``GET /metrics``
        serves); a test passes its own throw-away set built from a fresh
        ``CollectorRegistry`` instead, so recorded values are isolated and
        assertable without touching process-wide state.
    model_prices:
        Issue #166 -- the price table each turn's ``TurnUsage.cost_usd`` is
        computed from, keyed by provider model id (see ``run_turn``'s
        ``model_id``). ``None`` (default) means no prices: every turn's cost
        is ``None``, never a guessed number. ``create_app``'s lifespan fills
        it from ``MODEL_PRICES`` (``Settings.model_prices``); a library user
        passes their own, or nothing. The runtime never reads the server
        ``Settings`` itself -- they fail closed without the server's
        environment.
    budget:
        ADR-011 part 5 (#252) -- per-user caps (`BudgetCaps`) for this runtime,
        set by the deployment; an agent definition cannot raise them. A turn
        whose subject has reached a cap is refused before any model call, and
        the cap is checked again before each later call of the turn. Needs a
        ``usage_ledger``; a USD cap needs a price for the runtime's model.
    usage_ledger:
        Where each turn's usage is recorded, per (runtime id, subject key). A
        turn with no subject is not recorded (it counts against the runtime
        only, in metrics). A failed write is logged and never fails the turn.
    user_memory:
        #250 -- the limits of per-user memory. With them, a turn that has a
        subject, on a definition whose ``memory_policy`` says
        ``user_memory: true``, gets the user's stored facts as an
        untrusted-data block (never as plain text). Needs ``scope_provider``
        and ``runtime_id``. A store that fails is logged and the turn goes on
        without the facts.
    execution_limits:
        #278 -- the deployment's ceiling for this runtime
        (``total_execution_timeout_s``, ``tool_call_timeout_s``,
        ``max_tool_calls``), like ``build_runtime(execution_limits=...)``;
        given to both, the tighter value of each key wins. It replaces the
        platform default as the ceiling: the definition's own value may
        tighten it, never exceed it. A turn must fit the outbox lease with
        ``LEASE_MARGIN_S`` to spare and a tool call must fit its turn, or
        construction raises ``ValueError`` naming the runtime. The effective
        limits are logged (``runtime.execution_limits``).
    timeout_reply:
        #278 -- the text a timed-out turn answers, sent as given (never
        templated with user or tool data). ``None``: ``DEFAULT_TIMEOUT_REPLY``.
    """

    def __init__(
        self,
        runtime: EquippedRuntime,
        model: BaseChatModel | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        runtime_id: str | None = None,
        metrics: Metrics | None = None,
        model_prices: Mapping[str, ModelPrice] | None = None,
        checkpointer_timeout_s: float = DEFAULT_CHECKPOINTER_TIMEOUT_S,
        scope_provider: ScopeProvider | None = None,
        budget: BudgetCaps | None = None,
        usage_ledger: UsageLedger | None = None,
        user_memory: UserMemoryLimits | None = None,
        execution_limits: Mapping[str, Any] | None = None,
        timeout_reply: str | None = None,
    ) -> None:
        if user_memory is not None and (scope_provider is None or runtime_id is None):
            raise ValueError(
                "an AgentRuntime with user_memory needs a scope_provider to keep "
                "the facts in and an explicit runtime_id to key them by"
            )
        if budget is not None and usage_ledger is None:
            raise ValueError("a budget needs a usage_ledger to count usage in")
        if usage_ledger is not None and runtime_id is None:
            raise ValueError(
                "an AgentRuntime with a usage_ledger needs an explicit runtime_id; "
                "the default is the model name"
            )
        if scope_provider is not None and runtime_id is None:
            # ADR-011 part 1: per-user data is keyed by the runtime id, and
            # the fallback is the model's display name, which two agents on
            # one model share. Refuse rather than mix their users' data.
            raise ValueError(
                "an AgentRuntime with a scope_provider (any per-user store) "
                "needs an explicit runtime_id; the default is the model name"
            )
        if not checkpointer_timeout_s > 0:
            raise ValueError(
                f"checkpointer_timeout_s must be positive, got {checkpointer_timeout_s!r}"
            )
        if model is None:
            model = build_agent_model(runtime.definition)
        else:
            _report_model_conflict(runtime.definition, model)
        self._runtime = runtime
        self._equipped = runtime
        self._checkpointer = checkpointer
        self._checkpointer_timeout_s = float(checkpointer_timeout_s)
        self._schemas = [spec.to_langchain_tool_schema() for spec in runtime.tools]
        # Only call bind_tools when there are tools to bind — some fake models
        # raise NotImplementedError for bind_tools even with an empty list.
        self._bound_model = model.bind_tools(self._schemas) if self._schemas else model
        # #78 Phase 0 (review finding 4) -- derived from the ORIGINAL,
        # unbound `model` (bind_tools's return value, e.g. a RunnableBinding,
        # no longer exposes `model_name`/`model`). This runtime's own default
        # model_prices lookup key: a caller's `run_turn(model_id=...)`
        # is at most an optional override, never required for this runtime's
        # usage to be priced.
        self._model_id = model_display_name(model)
        # #78 Phase 0 Slice 2 -- see the `runtime_id`/`metrics` parameter
        # docs above.
        self._runtime_id = runtime_id if runtime_id is not None else self._model_id
        # #278 -- the deployment's ceiling, from build_runtime and from here.
        who = f"runtime {self._runtime_id!r}"
        self._deployment_limits = _tighter(
            validate_execution_limits(
                getattr(runtime, "deployment_limits", None), who=who
            ),
            validate_execution_limits(execution_limits, who=who),
        )
        self._timeout_reply = (
            DEFAULT_TIMEOUT_REPLY
            if timeout_reply is None
            else validate_timeout_reply(timeout_reply, who=who)
        )
        effective = self.execution_limits
        if self._deployment_limits:
            # Each ceiling was valid alone; what runs is their combination
            # with the definition's own values.
            check_effective_limits(effective, who=who)
        logger.info(
            "runtime.execution_limits",
            runtime_id=self._runtime_id,
            **{key: effective[key] for key in DEPLOYMENT_LIMIT_KEYS},
            deployment_set=sorted(self._deployment_limits),
        )
        self._scope_provider = scope_provider
        self._metrics = metrics if metrics is not None else DEFAULT_METRICS
        # Issue #166 -- copied, so a caller mutating its own dict later cannot
        # reprice turns behind this runtime's back.
        self._model_prices: dict[str, ModelPrice] = dict(model_prices or {})
        if budget is not None:
            budget.validate(f"budget for runtime {self._runtime_id!r}")
        if (
            budget is not None
            and budget.has_usd_cap
            and self._model_id not in self._model_prices
        ):
            # A USD cap cannot be enforced without a price, and pricing by
            # guessing is never done: fail at boot, not at the first turn.
            raise ValueError(
                f"a USD budget for runtime {self._runtime_id!r} needs a price for "
                f"model {self._model_id!r} in model_prices (MODEL_PRICES)"
            )
        self._budget = budget
        self._usage_ledger = usage_ledger
        self._user_memory = user_memory
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
    def execution_limits(self) -> dict[str, Any]:
        """The limits this runtime's turns run with: the definition's, over
        the deployment's ceiling (#278), over the platform defaults."""
        return _effective_limits(
            getattr(self._equipped.definition, "execution_limits", None),
            self._deployment_limits,
        )

    @property
    def permissions(self) -> tuple[str, ...]:
        """The role's own DECLARED permission set (``definition.permissions``).

        NOT the deploy-time grant — see ``EquippedRuntime.deploy_grant_ceiling``
        for what this runtime was actually granted (issue #38). A role
        merely declaring a permission is not the same as a deployment
        granting it; ``run_turn``'s own default no longer sources from this
        property (design.md Resolved Decision 5).
        """
        return self._equipped.definition.permissions

    @property
    def persist_conversation(self) -> bool:
        """Whether the definition's ``memory_policy`` lets a conversation be
        durably persisted (#26). False also when the policy does not parse.
        A live projection of the definition, like ``permissions``."""
        return persists_conversation(self._equipped.definition.memory_policy)

    @property
    def untrusted_input(self) -> bool:
        """Whether the resolved definition treats this runtime's input as
        untrusted (ADR-002 C.11). Sourced from the same `AgentDefinition`
        `permissions` reads above — a live projection, not a copy taken at
        construction time. `create_app`'s boot-time channel check (ADR-002
        C.13) reads this to refuse booting a channel bound to a role that is
        not marked safe for untrusted external input.
        """
        return self._equipped.definition.untrusted_input

    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
        model_id: str | None = None,
        *,
        subject: TurnSubject | None = None,
        actor: TurnActor | None = None,
        untrusted_data: Sequence[UntrustedData] = (),
    ) -> list[AnyMessage]:
        """Execute one conversational turn; return only the message list.

        Delegates to `run_turn_with_usage` (issue #78 review finding 5) and
        returns its `.messages` -- kept for every existing caller that only
        ever needed a `list[AnyMessage]` (`evals/runner.py`'s call sites that
        don't read usage, `services/webhook_worker.py`, the wider test
        suite). A caller that also wants this turn's real `TurnUsage` calls
        `run_turn_with_usage` instead -- see its docstring for the full
        parameter reference, unchanged here.
        """
        result = await self.run_turn_with_usage(
            messages,
            session_id,
            permissions=permissions,
            thread_id=thread_id,
            model_id=model_id,
            subject=subject,
            actor=actor,
            untrusted_data=untrusted_data,
        )
        return result.messages

    async def run_turn_with_usage(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
        model_id: str | None = None,
        *,
        subject: TurnSubject | None = None,
        actor: TurnActor | None = None,
        untrusted_data: Sequence[UntrustedData] = (),
    ) -> TurnResult:
        """Execute one conversational turn.

        ``subject`` / ``actor`` (ADR-011) name the end user the turn is for
        and who started it. The entry point binds them, never the model; both
        default to ``None`` and a turn without them behaves as before. They
        are visible for the duration of the turn to audit events and turn
        logs (subject key only, never the address).

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
            ``None``, in which case the equipped runtime's persisted deploy
            grant ceiling (``EquippedRuntime.deploy_grant_ceiling``, issue
            #38) is used — never the role's full declared permission set
            (``self.permissions``) — so Layer-2 cannot widen back out past
            what this runtime was actually granted at boot. This is the
            correct default for every entry point that has no separate
            identity of its own (OpenAI adapter, WhatsApp webhook).
        thread_id:
            Opt-in checkpointer key (design AD-1), e.g. the client's
            normalized phone number. ``None`` (default) keeps this call fully
            stateless — no checkpointer is engaged even if one is configured
            on this runtime. Ignored (treated as stateless) if this runtime
            was constructed without a ``checkpointer``.
        model_id:
            OPTIONAL OVERRIDE (issue #78 review finding 4) of the model id
            this turn's usage is priced under, looked up in this runtime's
            ``model_prices``. ``None`` (default) uses this
            runtime's OWN derived default instead --
            ``model_display_name(model)``, computed once from the provider
            model this ``AgentRuntime`` was constructed with (e.g.
            ``"gpt-4o"``, ``"deepseek/deepseek-v4-flash"``) -- so a caller
            that never names a model id (the WhatsApp webhook worker) still
            gets priced correctly, keyed by the actual provider model
            billing the tokens rather than a caller-chosen routing id. Pass
            an explicit value only to price under a different id than this
            runtime's own model (e.g. a live-eval comparing several runtime
            configurations under one label).
        untrusted_data:
            Issue #258 (ADR-011 part 4) -- stored or external data for this
            turn, the only way such data reaches the model. Rendered once per
            turn next to the system prompt, delimited by a random boundary,
            capped, counted against ``max_context_tokens`` and never
            persisted. Never policy: it changes no tool, grant or limit.

        Returns
        -------
        TurnResult
            ``messages``: a real ``list[AnyMessage]`` of every message
            accumulated during this turn (input + model responses + tool
            messages). ``usage``: this turn's real ``TurnUsage`` (tokens +
            cost, honestly ``None`` where unknown — see ``TurnUsage``'s
            docstring). When stateless, the caller owns cross-turn
            aggregation; when a checkpointer is engaged, the checkpointer
            owns cross-turn accumulation and ``messages`` already reflects
            it.
        """
        with turn_scope(subject, actor):
            return await self._run_turn_scoped(
                messages,
                session_id,
                permissions=permissions,
                thread_id=thread_id,
                model_id=model_id,
                untrusted_data=untrusted_data,
            )

    def _may_read_user_memory(self, permissions: Sequence[str] | None) -> bool:
        """Whether the deploy grant ceiling, narrowed by the turn, covers the read.

        The same coverage rule the interceptor applies to ``list_user_facts``:
        injecting the facts is a read of them, so a deployment that did not
        grant ``read:user_memory`` (or revoked it) gets no injection either.
        """
        granted = self._equipped.deploy_grant_ceiling
        if permissions is not None:
            current: set[type] = set()
            for name in permissions:
                try:
                    current.add(permission_registry.resolve(name))
                except UnknownPermissionNameError:
                    continue
            granted = granted & current
        needed = permission_registry.resolve("read:user_memory")
        return any(covers(cls, needed) for cls in granted)

    async def _user_memory_blocks(
        self, permissions: Sequence[str] | None = None
    ) -> list[UntrustedData]:
        """The turn's user-facts block, or none (#250).

        Only for a turn with a subject, on a definition that opted in, whose
        grants cover ``read:user_memory``. Never fails the turn: a store that
        cannot be read means a turn without the facts, logged.
        """
        subject = current_subject()
        if (
            self._user_memory is None
            or self._scope_provider is None
            or subject is None
            or not allows_user_memory(self._equipped.definition.memory_policy)
            or not self._may_read_user_memory(permissions)
        ):
            return []
        try:
            facts = UserFacts(
                backend_for(self._scope_provider.scope(self._runtime_id, subject.key)),
                self._user_memory,
            )
            block = await facts.injection_block(mark_used=True)
        except Exception:
            logger.warning("user_memory.inject_failed", exc_info=True)
            return []
        return [block] if block is not None else []

    async def _run_turn_scoped(
        self,
        messages: list[AnyMessage],
        session_id: str,
        *,
        permissions: tuple[str, ...] | None,
        thread_id: str | None,
        model_id: str | None,
        untrusted_data: Sequence[UntrustedData],
    ) -> TurnResult:
        """The turn itself, run inside `turn_scope`."""
        # #78 Phase 0 (review finding 4) -- an explicit override always wins;
        # otherwise fall back to this runtime's own derived provider model id.
        effective_model_id = model_id if model_id is not None else self._model_id
        effective_permissions = (
            permissions
            if permissions is not None
            # issue #38 — default to the persisted deploy grant ceiling, not
            # the role's full declared permission set (self.permissions):
            # Layer-2 must not be able to widen back out past what this
            # runtime was actually granted at boot.
            else tuple(
                sorted(
                    permission_registry.reverse(cls)
                    for cls in self._equipped.deploy_grant_ceiling
                )
            )
        )
        effective_limits = self.execution_limits
        max_tool_calls = effective_limits["max_tool_calls"]
        turn_start = time.monotonic()
        untrusted_data = [
            *untrusted_data,
            *await self._user_memory_blocks(effective_permissions),
        ]
        if (
            self._budget is not None
            and self._budget.has_usd_cap
            and effective_model_id not in self._model_prices
        ):
            # The boot guard only covers the runtime's own model; a per-turn
            # override without a price would make every cost null and the USD
            # cap unreachable. Refuse before any model call.
            raise ValueError(
                f"a USD budget for runtime {self._runtime_id!r} needs a price for "
                f"model {effective_model_id!r} in model_prices (MODEL_PRICES)"
            )
        gate = self._budget_gate(effective_model_id)
        if gate is not None:
            refusal = await gate(None)
            if refusal is not None:
                return self._finish_turn(
                    [*messages, AIMessage(content=refusal)],
                    usage=TurnUsage(0, 0, 0, 0, 0.0),
                    session_id=session_id,
                    model_id=effective_model_id,
                    outcome="budget_refused",
                    duration_s=time.monotonic() - turn_start,
                    budget_refused=True,
                )
        graph = _build_graph(
            self._equipped,
            self._bound_model,
            effective_permissions,
            max_tool_calls=max_tool_calls,
            tool_call_timeout_s=effective_limits["tool_call_timeout_s"],
            metrics=self._metrics,
            max_context_tokens=int(effective_limits["max_context_tokens"]),
            scope_provider=self._scope_provider,
            runtime_id=self._runtime_id,
            budget_gate=gate,
            # Never more than the context budget itself (about 4 characters a
            # token): an over-budget request is never built from the blocks.
            untrusted_text=render_untrusted_blocks(
                untrusted_data,
                total_max_chars=min(
                    DEFAULT_TURN_MAX_CHARS,
                    max(1, int(effective_limits["max_context_tokens"]) * 4),
                ),
            ),
        )
        # Issue #24 -- a thread's history is read here, before the turn, and
        # the turn is saved once at the end (see `_Thread` for the failure
        # policy). The graph itself always runs without a checkpointer.
        # Neither the read nor the save counts against the turn budget: the
        # budget bounds the agent's own work, and each of them has its own
        # bound, `checkpointer_timeout_s`.
        thread = (
            _Thread(
                graph.compile(checkpointer=self._checkpointer),
                thread_id,
                timeout_s=self._checkpointer_timeout_s,
                metrics=self._metrics,
            )
            if thread_id is not None
            and self._checkpointer is not None
            and self.persist_conversation
            else None
        )
        history = await thread.load() if thread is not None else []
        # D-014 S4 (design AD-1): the system prompt is no longer prepended
        # here — _call_model prepends it to the MODEL INPUT on every call and
        # never persists it in state (see _call_model docstring).
        all_messages = [*history, *messages]
        initial_state: AgentState = {
            "messages": all_messages,
            "session_id": session_id,
            "current_permissions": effective_permissions,
            # Always set explicitly (not inherited from a prior checkpoint):
            # a per-turn budget must reset to 0 even when this thread resumes
            # from persisted state (design AD-1 gotcha).
            "tool_call_count": 0,
            # #78 Phase 0 -- same reset-per-turn reasoning as tool_call_count
            # above: a resumed checkpointed thread must never inherit a
            # prior turn's usage entries.
            "turn_usage": [],
        }
        # D-014 S2 (design AD-3): a hard recursion_limit backstop derived from
        # max_tool_calls. LangGraph's own default (25 super-steps) is too low
        # for a legitimately full-budget turn (roughly 2 steps per tool call
        # plus the final call_model step) — without this override a turn that
        # stays exactly within its own max_tool_calls budget could still crash
        # with GraphRecursionError instead of completing normally.
        recursion_limit = 2 * max_tool_calls + 10
        # #78 Phase 0 Slice 2 -- measured across the whole call (graph build
        # through the final _finish_turn below), the single choke point both
        # the "ok" and "timeout" outcomes already return through. The outer
        # `except Exception` records "error" for anything neither branch
        # below catches (e.g. a genuine provider failure) and then re-raises
        # unchanged -- this adds observability only, it does not change what
        # was already unhandled before this metric existed. A checkpointer
        # failure never reaches it: `_Thread` handles every one.
        progress = _TurnProgress()
        try:
            try:
                async with asyncio.timeout(
                    effective_limits["total_execution_timeout_s"]
                ):
                    result = await _run_tracking_progress(
                        graph.compile(),
                        initial_state,
                        {"recursion_limit": recursion_limit},
                        progress,
                    )
            except TimeoutError:
                logger.warning(
                    "runtime.timeout",
                    total_execution_timeout_s=effective_limits[
                        "total_execution_timeout_s"
                    ],
                )
                # D-007: record runtime_timeout event
                _emit(
                    "record_runtime_timeout",
                    definition=self._equipped.definition,
                    total_execution_timeout_s=effective_limits[
                        "total_execution_timeout_s"
                    ],
                )
                # Issue #172 -- the cancelled invocation returns nothing, but
                # `progress` holds the state after the last step that
                # completed: keep its messages (the tool calls already made)
                # and account for the model calls that returned. A request
                # still in flight reported no usage and is not counted (see
                # `TurnUsage`).
                partial = progress.state
                if partial is not None:
                    partial_messages = list(partial["messages"])
                    raw_partial_usage = partial.get("turn_usage", [])
                else:
                    partial_messages = all_messages
                    raw_partial_usage = []
                reason = (
                    "the turn timed out after "
                    f"{float(effective_limits['total_execution_timeout_s'])}s"
                )
                # #278 -- the deployment's text, as configured: never
                # formatted with anything from the turn.
                timeout_reply = AIMessage(content=self._timeout_reply)
                if thread is not None:
                    # Issue #24 -- the thread keeps the turn as the user got
                    # it: its interrupted tool calls answered, then the reply.
                    await thread.save(
                        _close_unanswered_tool_calls(
                            partial_messages[len(history) :], reason=reason
                        )
                        + [timeout_reply]
                    )
                timed_out_result = self._finish_turn(
                    _close_unanswered_tool_calls(partial_messages, reason=reason)
                    + [timeout_reply],
                    usage=_aggregate_turn_usage(raw_partial_usage),
                    session_id=session_id,
                    model_id=effective_model_id,
                    outcome="timeout",
                    duration_s=time.monotonic() - turn_start,
                    timed_out=True,
                    timeout_s=float(effective_limits["total_execution_timeout_s"]),
                )
                await self._record_usage(
                    timed_out_result,
                    effective_model_id,
                    _count_tool_messages(partial_messages[len(all_messages) :]),
                )
                return timed_out_result
        except Exception:
            record_turn(
                self._metrics,
                runtime_id=self._runtime_id,
                outcome="error",
                duration_s=time.monotonic() - turn_start,
            )
            raise
        final_messages = list(result["messages"])
        if thread is not None:
            await thread.save(final_messages[len(history) :])
        finished = self._finish_turn(
            final_messages,
            usage=_aggregate_turn_usage(result.get("turn_usage", [])),
            session_id=session_id,
            model_id=effective_model_id,
            outcome="budget_refused" if result.get("budget_refused") else "ok",
            duration_s=time.monotonic() - turn_start,
            budget_refused=bool(result.get("budget_refused")),
        )
        await self._record_usage(
            finished,
            effective_model_id,
            _count_tool_messages(final_messages[len(all_messages) :]),
        )
        return finished

    def _budget_gate(self, model_id: str | None) -> BudgetGate | None:
        """The per-user cap check for the running turn, or ``None`` without caps.

        Called with the graph state (``None`` before the graph ran); the usage
        the turn already made is added to the ledger's totals, because the
        ledger is only written when the turn ends.
        """
        caps, ledger, subject = self._budget, self._usage_ledger, current_subject()
        if caps is None or ledger is None or subject is None:
            return None
        runtime_id = self._runtime_id

        async def gate(state: AgentState | None) -> str | None:
            pending = UsageTotals()
            if state is not None:
                usage = _aggregate_turn_usage(state.get("turn_usage", []))
                cost = _compute_turn_cost(usage, model_id, self._model_prices)
                pending = UsageTotals(
                    tokens=usage.total_tokens or 0, cost_usd=cost or 0.0
                )
            reached = await reached_cap(
                ledger, caps, runtime_id, subject.key, pending=pending
            )
            if reached is None:
                return None
            cap, limit, used = reached
            logger.info("runtime.budget_refused", cap=cap, used=used, limit=limit)
            _emit(
                "record_budget_refused",
                definition=self._equipped.definition,
                runtime_id=runtime_id,
                cap=cap,
                limit=limit,
                used=used,
            )
            return caps.message

        return gate

    async def _record_usage(
        self, result: TurnResult, model_id: str | None, tool_calls: int
    ) -> None:
        """Write the turn to the ledger, where it was priced (ADR-011 part 5).

        Only a turn with a subject is recorded. The turn is already paid for,
        so a failed write is logged and never loses the reply.
        """
        subject = current_subject()
        if self._usage_ledger is None or subject is None:
            return
        usage = result.usage
        try:
            await self._usage_ledger.record(
                UsageRecord(
                    runtime_id=self._runtime_id,
                    subject_key=subject.key,
                    actor=_actor_label(),
                    model_id=model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    cost_usd=usage.cost_usd,
                    model_calls=usage.model_calls,
                    tool_calls=tool_calls,
                    timed_out=result.timed_out,
                )
            )
        except Exception:
            logger.exception("runtime.usage_record_failed")

    def _finish_turn(
        self,
        messages: list[AnyMessage],
        *,
        usage: TurnUsage,
        session_id: str,
        model_id: str | None,
        outcome: str,
        duration_s: float,
        timed_out: bool = False,
        timeout_s: float | None = None,
        budget_refused: bool = False,
    ) -> TurnResult:
        """Attach real cost to *usage*, log one `runtime.turn_usage` event
        (issue #78 Phase 0), record this turn's metrics (issue #78 Phase 0
        Slice 2), and return `messages`/`usage` as a `TurnResult`.

        The log call carries no explicit correlation id: it reuses this
        module's existing `structlog` logger, which already picks up
        whatever `request_id`/`thread_id` contextvars the caller bound
        (`observability/middleware.py`'s `RequestIdMiddleware` for the
        adapter path) — the same mechanism every other `runtime.*` log line
        in this file already relies on.

        Both callers of this method (the "ok" and "timeout" return points in
        `run_turn_with_usage`) are this turn's single choke point for
        `record_turn` -- see that method's own comment for the "error"
        outcome, which never reaches here (it is recorded and re-raised
        before this method could be called).
        """
        cost_usd = _compute_turn_cost(usage, model_id, self._model_prices)
        usage = dataclasses.replace(usage, cost_usd=cost_usd)
        logger.info(
            "runtime.turn_usage",
            session_id=session_id,
            subject=_subject_key(),
            actor=_actor_label(),
            model_id=model_id,
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cost_usd=usage.cost_usd,
        )
        record_turn(
            self._metrics,
            runtime_id=self._runtime_id,
            outcome=outcome,
            duration_s=duration_s,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            actor_kind=_actor_kind(),
        )
        return TurnResult(
            messages=messages,
            usage=usage,
            timed_out=timed_out,
            timeout_s=timeout_s,
            budget_refused=budget_refused,
        )
