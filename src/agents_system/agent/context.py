"""Context-size control for the model input (issue #28, ADR-002 G.24).

`trim_history` decides which messages a model call is shown. It is the first
mechanism ADR-002 G.24 asks for: TRIMMING, not summarization. Dropping the
oldest messages is deterministic, free, cannot invent facts, and needs no
extra model call. Compaction (replacing old turns with a summary) stays a
follow-up: it costs a provider call per compaction and can distort what the
customer said, so it should be built on evidence that trimming loses
something that matters.

The function is pure. It works on the model-input projection built in
`agent.graph._call_model` and never touches the persisted history.

Rules, in priority order:

1. The system prompt and the tool schemas (`fixed_tokens`) are always sent.
2. The latest user message is always kept.
3. An assistant message with tool calls and its tool replies are one unit:
   a unit is kept whole or dropped whole (issue #181: a tool call without its
   reply, or a reply without its call, is rejected by providers).
4. Of the rest, the newest messages win: the history is a contiguous suffix,
   and it starts on a user message, because some providers reject a
   conversation that opens on an assistant turn.

When rules 1 to 3 alone already exceed the budget, the input is sent above
it and `ContextTrim.over_budget` says so; the alternative, dropping the
user's question, would answer nothing.

Token counts are an ESTIMATE (`count_tokens_approximately`, about four
characters a token). No tokenizer covers every configured provider, so the
default budget leaves a wide margin instead of pretending to be exact.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

#: The context window the default budget is derived from. It is deliberately
#: the SMALLEST window among the hosted models this platform is run with
#: (128k tokens), not the reference model's: a role that runs a model with a
#: larger window still gets a safe budget, and a smaller local model lowers
#: `max_context_tokens` itself.
ASSUMED_CONTEXT_WINDOW_TOKENS = 128_000

#: The share of the window the input may use. The rest is headroom for the
#: reply, for the error of a four-characters-a-token estimate (Spanish text
#: and JSON tool output run denser than English prose), and for provider
#: framing the estimate does not see.
CONTEXT_BUDGET_FRACTION = 0.5

#: Platform default for `execution_limits.max_context_tokens`.
DEFAULT_MAX_CONTEXT_TOKENS = int(
    ASSUMED_CONTEXT_WINDOW_TOKENS * CONTEXT_BUDGET_FRACTION
)


def estimate_tokens(messages: Sequence[AnyMessage]) -> int:
    """Estimated token count of *messages* (about four characters a token)."""
    return count_tokens_approximately(list(messages)) if messages else 0


@dataclasses.dataclass(frozen=True)
class ContextTrim:
    """The outcome of `trim_history`.

    `messages` is the history to send (without the system prompt). Token
    figures include `fixed_tokens`, so `tokens_after` is the estimated size
    of the whole request the budget applies to.
    """

    messages: list[AnyMessage]
    dropped_messages: int
    tokens_before: int
    tokens_after: int
    #: Rules 1 to 3 alone exceed the budget: the input is sent above it.
    over_budget: bool

    @property
    def trimmed(self) -> bool:
        return self.dropped_messages > 0


def _units(history: Sequence[AnyMessage]) -> list[list[AnyMessage]]:
    """Group *history* into indivisible units (rule 3).

    An assistant message with tool calls takes the tool replies right after
    it. Every other message is a unit of its own.
    """
    units: list[list[AnyMessage]] = []
    index = 0
    while index < len(history):
        message = history[index]
        unit = [message]
        index += 1
        if isinstance(message, AIMessage) and message.tool_calls:
            while index < len(history) and isinstance(history[index], ToolMessage):
                unit.append(history[index])
                index += 1
        units.append(unit)
    return units


def trim_history(
    history: Sequence[AnyMessage], *, max_tokens: int, fixed_tokens: int = 0
) -> ContextTrim:
    """Trim *history* so the request fits *max_tokens*, oldest messages first.

    *fixed_tokens* is what the request carries besides the history (system
    prompt, tool schemas). See the module docstring for the rules.
    """
    messages = list(history)
    history_tokens = estimate_tokens(messages)
    tokens_before = fixed_tokens + history_tokens
    if tokens_before <= max_tokens or not messages:
        return ContextTrim(
            messages=messages,
            dropped_messages=0,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            over_budget=False,
        )

    units = _units(messages)
    costs = [estimate_tokens(unit) for unit in units]
    # Rule 2: the anchor is the latest user message. Without any user
    # message, the newest unit stands in for it.
    anchor = next(
        (
            i
            for i in range(len(units) - 1, -1, -1)
            if isinstance(units[i][0], HumanMessage)
        ),
        len(units) - 1,
    )
    remaining = max_tokens - fixed_tokens - costs[anchor]

    # The current turn: what follows the user message, newest first.
    tail_start = len(units)
    while tail_start > anchor + 1 and costs[tail_start - 1] <= remaining:
        tail_start -= 1
        remaining -= costs[tail_start]

    # Earlier history only when the whole current turn fit.
    head_start = anchor
    if tail_start == anchor + 1:
        while head_start > 0 and costs[head_start - 1] <= remaining:
            head_start -= 1
            remaining -= costs[head_start]
        # Rule 4: start on a user message.
        while head_start < anchor and not isinstance(
            units[head_start][0], HumanMessage
        ):
            head_start += 1

    kept_units = [*units[head_start : anchor + 1], *units[tail_start:]]
    kept = [message for unit in kept_units for message in unit]
    tokens_after = fixed_tokens + estimate_tokens(kept)
    return ContextTrim(
        messages=kept,
        dropped_messages=len(messages) - len(kept),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        over_budget=tokens_after > max_tokens,
    )
