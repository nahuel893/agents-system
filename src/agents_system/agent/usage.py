"""Turn usage and cost: the per-turn token counts, the result of a turn and the model name shown in audits."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AnyMessage,
)
from langchain_core.messages.ai import UsageMetadata

from agents_system.config import ModelPrice

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
