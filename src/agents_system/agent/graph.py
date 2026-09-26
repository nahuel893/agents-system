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
import dataclasses
import json
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from functools import partial
from typing import Any

import redis.exceptions
import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from agents_system.agent.state import AgentState
from agents_system.config import Settings, get_settings
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.injector import _emit
from agents_system.harness.interceptor import CallResult, PolicyViolation, intercept
from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    Metrics,
    record_limit_trip,
    record_tool_call,
    record_turn,
)
from agents_system.permissions.permission_registry import permission_registry

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


# ---------------------------------------------------------------------------
# #78 Phase 0 — real per-turn token usage and cost
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TurnUsage:
    """Real per-turn token usage and cost -- never an invented number.

    Every field reflects only what the provider actually reported through
    `AIMessage.usage_metadata`, summed across every `call_model` invocation
    this turn made (a turn can make several when the model uses tools).

    - `model_calls` is `None` only when it could not be determined at all
      (the turn timed out before `run_turn` could account for it) -- never
      confused with a genuine `0`.
    - `input_tokens`/`output_tokens`/`total_tokens` are `None` whenever ANY
      of this turn's model calls reported no `usage_metadata`: a partial sum
      is never reported as if it were the whole turn's total.
    - `cost_usd` is `None` whenever any token total above is `None`, no
      `model_id` was given, or `model_id` has no entry in
      `Settings.model_prices` -- a missing price is never guessed at.
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

    This is `AgentRuntime`'s own default `Settings.model_prices` lookup key
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
    usage: TurnUsage, model_id: str | None, settings: Settings
) -> float | None:
    """Cost in USD for *usage*, from `Settings.model_prices` only.

    Returns `None` -- never a guessed number, and never a crash -- whenever
    `model_id` is unset, the token totals themselves are unknown or outside
    the plausible range a real provider could report (see
    `_MAX_PLAUSIBLE_TURN_TOKENS`), or `model_id` has no entry in
    `Settings.model_prices` (design AD note: a price table is opt-in per
    model id, not a global default).
    """
    if model_id is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    if not _is_plausible_token_count(
        usage.input_tokens
    ) or not _is_plausible_token_count(usage.output_tokens):
        return None
    price = settings.model_prices.get(model_id)
    if price is None:
        return None
    return (
        usage.input_tokens / 1_000_000 * price.input_per_million
        + usage.output_tokens / 1_000_000 * price.output_per_million
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
    retried_after_tool_format_error = False
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
            metrics=metrics,
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

    Review finding 3 (PR #87) — usage under checkpointer degradation: the
    FAILED invocation above may already have made one or more real provider
    calls before the checkpointer backend error surfaced (LangGraph writes a
    checkpoint after each super-step, so a failure there can follow a
    completed ``call_model``). ``ainvoke`` raises without returning any
    partial state, so exactly how many calls (and their tokens) is
    unrecoverable. The retried invocation below runs the SAME turn from
    scratch and its own ``turn_usage`` would silently miss whatever the
    failed attempt already spent — reporting only the retry's numbers would
    be a plausible-looking undercount, not an honest one. The chosen fix:
    when degradation occurred, the returned ``turn_usage`` is the sentinel
    ``None`` (distinct from a real, possibly-empty list) instead of
    whatever the retried invocation recorded — ``run_turn_with_usage`` reads
    that sentinel as "genuinely unknown" and reports ``_UNKNOWN_TURN_USAGE``
    (``model_calls=None``, same as the AD-3 timeout backstop), rather than a
    count that looks precise but is not.
    """
    checkpointer_degraded = False
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
            checkpointer_degraded = True

    compiled = graph.compile()
    result = dict(
        await compiled.ainvoke(
            initial_state, config={"recursion_limit": recursion_limit}
        )
    )
    if checkpointer_degraded:
        result["turn_usage"] = None
    return result


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
    """

    def __init__(
        self,
        runtime: EquippedRuntime,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        runtime_id: str | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._runtime = runtime
        self._equipped = runtime
        self._checkpointer = checkpointer
        self._schemas = [spec.to_langchain_tool_schema() for spec in runtime.tools]
        # Only call bind_tools when there are tools to bind — some fake models
        # raise NotImplementedError for bind_tools even with an empty list.
        self._bound_model = model.bind_tools(self._schemas) if self._schemas else model
        # #78 Phase 0 (review finding 4) -- derived from the ORIGINAL,
        # unbound `model` (bind_tools's return value, e.g. a RunnableBinding,
        # no longer exposes `model_name`/`model`). This runtime's own default
        # Settings.model_prices lookup key: a caller's `run_turn(model_id=...)`
        # is at most an optional override, never required for this runtime's
        # usage to be priced.
        self._model_id = model_display_name(model)
        # #78 Phase 0 Slice 2 -- see the `runtime_id`/`metrics` parameter
        # docs above.
        self._runtime_id = runtime_id if runtime_id is not None else self._model_id
        self._metrics = metrics if metrics is not None else DEFAULT_METRICS
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
        """The role's own DECLARED permission set (``definition.permissions``).

        NOT the deploy-time grant — see ``EquippedRuntime.deploy_grant_ceiling``
        for what this runtime was actually granted (issue #38). A role
        merely declaring a permission is not the same as a deployment
        granting it; ``run_turn``'s own default no longer sources from this
        property (design.md Resolved Decision 5).
        """
        return self._equipped.definition.permissions

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
        )
        return result.messages

    async def run_turn_with_usage(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
        model_id: str | None = None,
    ) -> TurnResult:
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
            this turn's usage is priced under, looked up in
            ``Settings.model_prices``. ``None`` (default) uses this
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
        effective_limits = _effective_limits(self._equipped.definition.execution_limits)
        max_tool_calls = effective_limits["max_tool_calls"]
        graph = _build_graph(
            self._equipped,
            self._bound_model,
            effective_permissions,
            max_tool_calls=max_tool_calls,
            tool_call_timeout_s=effective_limits["tool_call_timeout_s"],
            metrics=self._metrics,
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
        # below catches (e.g. a genuine provider failure, or a checkpointer
        # failure that is not a `redis.exceptions.RedisError`) and then
        # re-raises unchanged -- this adds observability only, it does not
        # change what was already unhandled before this metric existed.
        turn_start = time.monotonic()
        try:
            try:
                async with asyncio.timeout(
                    effective_limits["total_execution_timeout_s"]
                ):
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
                # #78 Phase 0 -- a timeout cancels the invocation before this
                # runtime can read back how many call_model invocations actually
                # completed, so `model_calls` (and every token total) is
                # genuinely UNKNOWN here -- never reported as 0.
                return self._finish_turn(
                    all_messages
                    + [
                        AIMessage(
                            content=(
                                "This is taking longer than expected. Please try again."
                            )
                        )
                    ],
                    usage=_UNKNOWN_TURN_USAGE,
                    session_id=session_id,
                    model_id=effective_model_id,
                    outcome="timeout",
                    duration_s=time.monotonic() - turn_start,
                )
        except Exception:
            record_turn(
                self._metrics,
                runtime_id=self._runtime_id,
                outcome="error",
                duration_s=time.monotonic() - turn_start,
            )
            raise
        # #78 Phase 0 (review finding 3) -- `turn_usage` is the sentinel
        # `None` (never a real, possibly-empty list) exactly when
        # `_ainvoke_with_optional_checkpointer` degraded past a checkpointer
        # backend failure: that failed attempt may have already spent real,
        # unrecoverable tokens the retried invocation's own turn_usage does
        # not include, so the whole turn's usage is honestly unknown rather
        # than a plausible-looking undercount.
        raw_turn_usage = result.get("turn_usage", [])
        usage = (
            _UNKNOWN_TURN_USAGE
            if raw_turn_usage is None
            else _aggregate_turn_usage(raw_turn_usage)
        )
        return self._finish_turn(
            list(result["messages"]),
            usage=usage,
            session_id=session_id,
            model_id=effective_model_id,
            outcome="ok",
            duration_s=time.monotonic() - turn_start,
        )

    def _finish_turn(
        self,
        messages: list[AnyMessage],
        *,
        usage: TurnUsage,
        session_id: str,
        model_id: str | None,
        outcome: str,
        duration_s: float,
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
        cost_usd = _compute_turn_cost(usage, model_id, get_settings())
        usage = dataclasses.replace(usage, cost_usd=cost_usd)
        logger.info(
            "runtime.turn_usage",
            session_id=session_id,
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
        )
        return TurnResult(messages=messages, usage=usage)
