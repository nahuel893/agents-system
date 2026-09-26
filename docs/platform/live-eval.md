# Live-eval pipeline

ADR-002 E.18 (`docs/architecture/adr-002-agent-model-and-capabilities.md`).
Deterministic per-PR tests (`build_test_registry`, fake models) prove the
*mechanism* works: a denied call is denied, a tool surface resolves as
declared. They cannot prove a role's *behavior* under a real model matches
what its prompt and policy intend. The live-eval pipeline in
`src/agents_system/evals/` closes that gap: it runs a role's scenario N times
against a real local model and reports a success rate, never a single
pass/fail — a probabilistic system's correctness is a rate, not a boolean.

This pipeline is **manual or nightly, never per-PR** (real model calls are
slow, cost money on a hosted model, and are not fully deterministic — the
wrong shape for a gate that blocks every PR).

## Running it

```bash
pytest -m live
```

`live` is a pytest marker, deselected by default exactly like `integration`
(`pyproject.toml`'s `addopts`). You need a running local Ollama with the
configured model already pulled:

```bash
ollama serve
ollama pull qwen2.5:3b   # or whatever OLLAMA_MODEL names
```

Point the pipeline at a different model or host with the same `Settings`
fields `_build_chat_model` already reads for every other role:

```bash
export OLLAMA_MODEL=qwen3:8b
export OLLAMA_BASE_URL=http://localhost:11434   # empty/unset = ChatOllama's own default
pytest -m live
```

Both default to what was hardcoded before #169 (`qwen2.5:3b`,
ChatOllama's own host resolution) — an unconfigured run is unchanged.

## Switching providers (EVAL_PROVIDER)

The eval's own provider is a separate switch from the running application's
`ADAPTER_PROVIDER`: `EVAL_PROVIDER` (`Settings.eval_provider`, default
`"ollama"`), read by `agents_system.evals.provider.build_eval_model()`. Choosing a
different model for a one-off eval run must never change what the app itself
serves, so the two never share a field. It accepts the same values
`main._build_chat_model` dispatches on: `ollama`, `groq`, `anthropic`,
`openai_compatible`.

`openai_compatible` reads the exact same `OPENAI_COMPATIBLE_BASE_URL` /
`OPENAI_COMPATIBLE_MODEL` / `OPENAI_COMPATIBLE_API_KEY` settings every other
role already uses — any OpenAI-compatible host works, including a hosted
router. OpenRouter, for example:

```bash
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_COMPATIBLE_MODEL=deepseek/deepseek-v4-flash
export OPENAI_COMPATIBLE_API_KEY=...   # your own key -- never commit it
pytest -m live
```

(Or keep these in your `.env` — `Settings` loads it automatically; never
print or commit that file.) The model name `ScenarioResult` reports is read
back from the constructed model itself
(`agents_system.evals.provider.model_display_name`): the ChatOpenAI-family
classes (`groq`, `openai_compatible`) expose it as `model_name`, not
`model` — reporting a hardcoded string here would silently drift from
whatever the provider actually used.

## Scenario schema

A scenario is one YAML file: a role, its input turns, and behavior
assertions — **never an assertion on the model's exact generated text**.
Real models are not deterministic enough for string-equality checks to be
meaningful or stable; every assertion here is behavioral instead (which tool
was called, whether a call was denied, whether escalation succeeded).

```yaml
role: sales-agent            # required — the role folder name
name: sales_agent_smoke      # optional — defaults to the file's stem
description: >               # optional, free text
  A customer asks about a catalog item.
client: deployment-id        # optional — resolve under a deployment; omit for the generic role
turns:                       # required, non-empty — user messages, sent in order
  - "Hi, do you have Item Alpha in stock and how much does it cost?"
assertions:
  tools_called: [catalog_search]                    # every name here MUST have been called
  tools_not_called: [order_writer, escalation_notifier]  # none of these may be called
  permission_denied: false                          # true/false: was any call blocked by the Layer-2 interceptor
  escalation_expected: false                         # true: a successful escalation_notifier call is required
                                                       # false: escalation_notifier must never be attempted
granted_permissions: [read:catalog]  # optional wire-name list; omit for the named all-declared compatibility default
```

Every `assertions` field is optional; an omitted field asserts nothing.

When `granted_permissions` is omitted, the runner applies and logs the named
`all-declared` compatibility default: every permission declared by the
resolved role is passed to `build_runtime`. This preserves existing scenario
YAML. When present, the wire-name list passes unmodified to `build_runtime`.
In both cases, the factory applies R3 coverage and R4 grant validation, then
uses the resulting grant ceiling for both the Layer-1 tool surface and the
Layer-2 default revalidation. A narrowed grant therefore excludes an
in-manifest tool from the model's equipped surface, and an attempted call to
that excluded tool is denied by Layer 2.
`tools_called`/`tools_not_called` check whether the model *attempted* the
call at all (regardless of outcome). `escalation_expected: true` is
stricter — it additionally requires the call actually succeeded (no
`error_kind` in the tool's JSON result), because "the model tried to
escalate but the channel wasn't configured" is not the same claim as "the
customer was actually escalated to a human". `permission_denied` matches
specifically a Layer-2 `PolicyViolation` block (`agent/graph.py`'s
`"Tool call blocked: ..."` message) — a timeout is a different failure and
does not count.

Each turn runs through `AgentRuntime.run_turn` exactly like a real caller —
tool calls within a turn are resolved automatically by the graph; `turns` are
only the user's own messages, sent one at a time with the runtime's
accumulated history fed back in.

Load one file with `agents_system.evals.schema.load_scenario(path)`, or every
`*.yaml`/`*.yml` file in a directory with `load_scenarios(directory)`.
Either raises `ScenarioError`, naming the offending file, on any structural
problem (missing `role`, empty `turns`, a non-boolean `escalation_expected`,
...).

## Where scenario files live

`evals/scenarios/*.yaml` at the repository root — tracked in git, one file
per scenario. `evals/scenarios/sales_agent_smoke.yaml` is the pipeline's own
smoke scenario (#169): it proves `resolve()` → `build_runtime()` →
`AgentRuntime.run_turn` works end to end against a real model. It is
deliberately not a correctness suite for `sales-agent` — per-role scenario
coverage is out of scope for #169 and belongs to the sibling issues under
#52.

## Where results go

`evals/results/` — **gitignored** (`.gitignore`'s `evals/results/` entry;
`evals/scenarios/` is a sibling, not a parent, and stays tracked). Each run
of `agents_system.evals.reporting.write_results(...)` writes two timestamped
files:

- `evals/results/<UTC timestamp>.json` — one entry per scenario: role,
  model, run count, pass count, success rate, per-run failure detail, and
  (issue #78 Phase 0) real token usage/cost -- see below.
- `evals/results/<UTC timestamp>.md` — a short markdown table (scenario,
  role, model, runs, success rate, tokens, cost) for a quick read.

## Tokens and cost (issue #78 Phase 0)

Every turn `AgentRuntime.run_turn_with_usage` makes carries its real
`AIMessage.usage_metadata`, summed across however many model calls that turn
made (a turn can make several when the model uses tools) into a `TurnUsage`
(`agents_system.agent.graph.TurnUsage`: `model_calls`, `input_tokens`,
`output_tokens`, `total_tokens`, `cost_usd`). Plain `run_turn` keeps
returning only the message list (`list[AnyMessage]`, unmodified for every
existing caller); `run_turn_with_usage` returns a `TurnResult(messages,
usage)` for a caller that also wants `.usage` -- see its docstring for why
this replaced an earlier `list` subclass. `run_scenario` calls
`run_turn_with_usage`, sums each run's turns into `RunOutcome.usage`, and
`ScenarioResult.total_usage` sums every run's usage into one scenario total
-- both `write_results` outputs report it (`total_tokens`/`total_cost_usd`
in the JSON, the `Tokens`/`Cost (USD)` columns in the markdown table).

**Honesty rule, not a shortcut**: a value is `None` (JSON) / `n/a`
(markdown) whenever it is genuinely unknown -- never a guessed `0`. If even
one of a turn's model calls reported no `usage_metadata`, that whole turn's
token totals are `None`; if even one turn/run in a sum is `None` (including a
run that crashed before completing a turn), the sum is `None` too --
`ScenarioResult.total_usage` is unknown as soon as ANY of its runs is,
never a partial sum across only the runs that succeeded. The same rule
applies one level down, at each individual model call: a turn's token
totals are `None` if even one of its calls reported a negative token count,
or a count at or above ~100M (an `openai_compatible` backend's
`usage_metadata` is untrusted input with no upstream size ceiling) --
checked per call, before summing, so one implausibly huge call and a
compensating negative one on the same turn cannot net out to a small,
plausible-looking total. `cost_usd` is additionally `None` whenever no
price is configured for the model id; an implausible count is treated the
same as an unknown one rather than raising.

**Configuring prices**: `Settings.model_prices` (env var `MODEL_PRICES`) is a
JSON object keyed by the PROVIDER MODEL id (e.g. `"gpt-4o"`,
`"deepseek/deepseek-v4-flash"`) -- never a caller-chosen routing id such as
a registered runtime id (the OpenAI adapter's request `model`) -- valued by USD
price per million input/output tokens:

```bash
export MODEL_PRICES='{"deepseek/deepseek-v4-flash": {"input_per_million": 0.14, "output_per_million": 0.28}}'
```

`AgentRuntime` derives this key itself, at construction time, from the
model it was actually built with (`model_display_name(model)`, the same
logic `evals/provider.py:model_display_name` re-exports) -- a caller never
has to name a model id for its turns to be priced. `run_turn(_with_usage)`'s
own `model_id` argument is at most an OPTIONAL OVERRIDE of that derived
default (e.g. a live-eval comparing several runtime configurations under
one shared label); the OpenAI adapter and the WhatsApp webhook worker both
rely on the derived default rather than overriding it, so every entry point
prices under the same, correct key. A model id with no entry here reports
`cost_usd: null` -- this is opt-in per model, never a global default rate.

**`POST /v1/chat/completions`'s `usage` field**: OpenAI SDK compatibility
(the official `openai` SDK's `CompletionUsage` requires non-Optional ints
for every field) means an unknown usage is reported as the top-level
`"usage": null`, never an object with `null` fields
(`{"prompt_tokens": null, ...}` fails client-side Pydantic validation).

## Turn duration (issue #78 Phase 0 Slice 2)

`run_scenario` times each turn's `run_turn_with_usage` call with real
wall-clock `time.monotonic()` and sums a run's turns into
`RunOutcome.duration_s`; `ScenarioResult.total_duration_s` sums every run's
duration into one scenario total. Both `write_results` outputs report it
(`total_duration_s` in the JSON, per-run `duration_s` in `run_details`, and
the `Duration (s)` column in the markdown table).

Same honesty posture as tokens/cost above: `duration_s` is `None` only for
a run that raised before its FIRST turn returned (there was nothing to
time yet) -- a run that crashes mid-scenario still reports the real,
partial sum of the turns that did complete before it failed.
`ScenarioResult.total_duration_s` is `None` as soon as any one run's
duration is unknown, the same coarser-grain rule `total_usage` already
applies.

**Per-tool-call durations were deliberately left out of this report** (the
issue's own "if cheap" qualifier): getting them into this per-run report
would need `TurnResult` to also carry a list of tool-call durations, which
this slice did not add. They are available instead via `/metrics`'s
`agent_tool_call_duration_seconds` histogram — see
`docs/platform/observability.md`, which also covers process memory/CPU,
the `agent_turns_total`/`agent_tool_calls_total`/`agent_limit_trips_total`
counters, and `GET /metrics`'s own protection model.

## Running it as a role's own pipeline

`agents_system.evals.runner.run_scenario(scenario, *, model, model_name,
registry, roots=None, runs=1)` resolves the scenario's role through the same
`resolve()`/`build_runtime()` path every other consumer uses (with the
role's own reference backends wired in the same way `tests/conftest.py`'s
`build_test_registry` does for tests), runs it `runs` times, and returns a
`ScenarioResult` carrying every run's outcome plus the aggregate
`success_rate`. `agents_system.evals.runner.evaluate_assertions(assertions,
messages)` is the assertion engine itself, callable directly against any
accumulated message transcript — this is what the offline unit tests in
`tests/test_eval_runner.py` exercise with a fake model, proving the runner's
own logic (scenario loading, assertion evaluation, rate aggregation, a
failing assertion counted as a failed run) without any network call.

`run_scenario` also registers a real `AuditSink` for the run (an in-memory
`_CapturingAuditSink` by default) so the platform's normal audit wiring
actually delivers instead of every event being silently dropped — a live
eval has no FastAPI lifespan to construct the app's own DB-backed sink.
`ScenarioResult.audit_events_captured` reports how many landed; whatever
sink was registered before the call, if any, is restored afterward. This is
diagnostic only — the `permission_denied` assertion still reads the
synchronous `ToolMessage` the interceptor returns, not the sink, since the
sink's own drainer batches on a 100ms timer and is only eventually
consistent by the time a run finishes.

## Local hardware note

An AMD RX 5700 XT (Navi10, `gfx1010`) needs the `ollama-vulkan` package
specifically — ROCm does not officially support `gfx1010`. `qwen2.5:3b` is
too small for reliable tool-calling in practice but is kept as a deliberate
floor/regression case: a model that fails some fraction of tool-calling
tasks is a useful signal that the harness is discriminating correctly, not a
bug in the pipeline.

## Cross-references

- ADR-002 E.18: `docs/architecture/adr-002-agent-model-and-capabilities.md`
- Reference backends the eval runner wires in: `docs/platform/reference-backends.md`
- Process/turn/tool metrics and `GET /metrics` (issue #78 Phase 0 Slice 2):
  `docs/platform/observability.md`
- Runner code: `src/agents_system/evals/{schema,runner,reporting,provider}.py`
- Offline tests: `tests/test_eval_schema.py`, `tests/test_eval_runner.py`,
  `tests/test_eval_reporting.py`, `tests/test_eval_provider.py`
- Live smoke test: `tests/test_live_eval_sales_agent.py`
