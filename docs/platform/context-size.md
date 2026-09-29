# Context-size control

Every model call carries the system prompt, the tool schemas and the
conversation history. Before issue #28 the history was sent whole, on every
call, however long it had grown. A long enough conversation would exceed the
model's context window and the provider would answer with a hard error, so
the agent stopped working exactly when the conversation mattered most.

The runtime now bounds the request. This is ADR-002 item G.24
(`docs/architecture/adr-002-agent-model-and-capabilities.md`).

## The decision: trimming first, compaction later

ADR-002 left the mechanism open. The decision, recorded here and in the ADR:
**trim, do not summarize**, as the first mechanism.

| | Trimming | Compaction (summarize old turns) |
|---|---|---|
| Cost | none | one extra provider call per compaction |
| Failure mode | old turns are forgotten | old turns are paraphrased, and a summary can state something nobody said |
| Determinism | fully deterministic, testable offline | depends on a model, so it needs live evaluation |
| Data available today | enough to pick a safe threshold | no evidence yet that trimming loses something that matters |

Compaction stays a follow-up. It should be built when there is evidence, from
real conversations, that trimming drops context an agent needed.

## What it does

`_call_model` (`agent/graph.py`) builds the model input in this order:
repair unanswered tool calls (#181), trim to the budget, prepend the system
prompt. `agent/context.py:trim_history` decides what is dropped. The rules,
in priority order:

1. The system prompt and the tool schemas are always sent.
2. The latest user message is always kept, and so is the newest unit after
   it: the tool call the model just made and its result. Without the result
   the model cannot tell the operation already ran and would issue it again.
3. An assistant message with tool calls and its tool replies are one unit,
   kept whole or dropped whole. A tool call without its reply, or a reply
   without its call, is rejected by providers (#181).
4. Of the rest, the newest messages win. The history that remains is a
   contiguous suffix and it starts on a user message, because some providers
   reject a conversation that opens on an assistant turn.

Trimming only shapes the **model-input projection**. `state["messages"]` is
never rewritten, so a checkpointed thread keeps its full history and the
messages a turn returns are unchanged.

A history that fits the budget is sent unmodified: no message is dropped and
nothing is logged.

If rules 1 to 3 alone exceed the budget (for example a single very large user
message, or a tool result larger than the budget), or the system prompt and
tool schemas alone do, the input is sent above the budget and flagged
`over_budget`. This holds even when nothing was dropped: a request that
cannot fit and has nothing to discard is still reported. Dropping the user's
question or the tool result would answer nothing, and the provider's own
error is then the honest failure.

During a long tool loop the same rules apply inside the current turn: the
newest tool round is always kept, then as many older ones as fit, the oldest
are dropped, and the user message stays.

## Configuration

The budget is `execution_limits.max_context_tokens`, per agent, in the role's
`policy.md`, a folder manifest, an `Agent(...)` parameter or a deployment
override:

```yaml
execution_limits:
  max_context_tokens: 24000   # a small local model
```

It follows every other execution limit:

- A missing or `null` value means the platform default.
- A definition can lower it and never raise it above its parent's value, or
  above the platform default when the parent sets none (`docs/platform/role.md`).
- It must be a finite, non-negative number. `0` is allowed and keeps only the
  fixed parts and the latest user message.

The budget counts the whole request: system prompt, tool schemas and history.
It does not count the model's reply.

### The default

`DEFAULT_MAX_CONTEXT_TOKENS` (`agent/context.py`) is **64,000**, derived from
the model's context window:

```
ASSUMED_CONTEXT_WINDOW_TOKENS (128,000) x CONTEXT_BUDGET_FRACTION (0.5)
```

- **128,000** is the smallest window among the hosted models this platform
  runs with, not the reference model's. A role on a model with a larger window
  still gets a safe budget.
- **0.5** is headroom, not a tuned number. Token counts are an estimate (about
  four characters a token, `langchain_core`'s `count_tokens_approximately`),
  and no tokenizer covers every configured provider. Spanish text and JSON tool
  output run denser than English prose. The rest of the window is also
  needed for the reply.

An agent on a smaller model (a local 8k or 32k model) must lower
`max_context_tokens` itself: the platform cannot see which window a configured
model has.

## Observability

A trimmed or over-budget model call is never silent:

- A `runtime.context_trimmed` warning with `dropped_messages`,
  `kept_messages`, `tokens_before`, `tokens_after`, `max_context_tokens` and
  `over_budget`. No message text.
- A `context_trimmed` audit event with the same counts (`docs/platform/audit.md`).
- One `agent_limit_trips_total{limit="max_context_tokens"}` sample
  (`docs/platform/observability.md`).

A model call that repeats inside one turn (a tool loop) reports each time it
trims.

## Baseline: what the measured data says

There is **no production traffic yet**, so this is not production data. It is
measured from what exists: the live-evaluation results in `evals/results/`
(297 scored runs over 33 scenarios) and the `runtime.turn_usage` log lines
kept under `.pi/ops/logs/` (238 distinct turns, mostly on the reference model
and one other hosted model).

| Measure | Median | p90 | Max |
|---|---|---|---|
| Tokens a whole turn consumed, input plus output (`evals/results/`, 297 runs) | 4,434 | 8,486 | 14,804 |
| Input tokens summed over a turn's model calls (logs, 238 turns) | 3,982 | 8,184 | 23,097 |
| Input tokens of one model call, averaged over its turn (logs) | 1,552 | 2,052 | 2,647 |
| Model calls in a turn (logs) | 3 | n/a | 9 |

Every scenario is a single user turn (median 62 characters, at most 211), so
nothing here measures a multi-turn conversation. What the data does say:

- One model call in these runs carries about 1.5k to 2.6k tokens, and most of
  that is the fixed part (system prompt and tool schemas), not conversation.
- The default budget, 64,000, is about 24 times the largest single request
  measured. It cannot trim any of the measured traffic, so it changes nothing
  for the scenarios in the suite.

What the data does **not** say is how fast a real conversation grows, or what
budget real conversations need. The default is therefore derived from the
model window (above), not from this baseline, and this baseline is only a check
that the default is nowhere near ordinary use. Once a channel has real traffic,
re-measure with the `runtime.context_trimmed` warning and the
`agent_limit_trips_total` counter: a budget that trims often is too small for
the conversations, a budget that never trims is either right or too generous.

## Not covered (follow-ups)

- **Compaction / summarization** of trimmed turns (see the decision above).
- **Persisted growth.** Trimming bounds what the model sees, not what the
  checkpointer stores. Since #24 the retention purge keeps only each thread's
  latest checkpoint and deletes a thread once it is idle for longer than
  `CHECKPOINTER_RETENTION_S`, but a very active thread still holds its whole
  history (`docs/platform/conversation-memory.md`).
- **A per-model window lookup** to derive the budget from the configured model
  automatically, instead of the fixed assumed window.
- **Exact token counting** per provider.
