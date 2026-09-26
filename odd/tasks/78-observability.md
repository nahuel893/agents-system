# #78 — Live-test Phase 0 — real observability

## Objective
Prove, with real numbers, what a turn against a real LLM costs and how it
performs, per `docs/delivery/live-test-plan.md`'s Phase 0 and Principle
("every assertion targets what the system did"). Phase 0 is delivered in
three slices; this document tracks all of them so a later slice resumes with
full context.

## Scope (this document covers all of #78; only Slice 1 is implemented so far)
- Slice 1 (this work unit): real token usage and cost per turn.
- Slice 2 (pending): process memory/CPU sampling, turn/tool durations, and
  exposing all of it via `/metrics` or `GET /health`.
- Slice 3 (pending): the real-app live harness (API, webhook, Postgres,
  Redis, audit-to-DB) that later phases (#79, #76-extends) build on.

Out of scope for Slice 1 (tracked as Slice 2/3 above): `/metrics`, process
memory/CPU, turn/tool durations, the real-app live harness.

## TDD mode
Strict TDD (per `.pi/ops/prompts/common-rules.txt` item 10 and the repo's
global CLAUDE.md). Runner: `pytest -q` from the worktree root with
`PATH=/home/nh/agents-system/.venv/bin:$PATH PYTHONPATH=<worktree>/src`.

## Slice 1 — real token usage and cost per turn

### Design
- `AgentRuntime.run_turn` sums `AIMessage.usage_metadata` across every
  `call_model` invocation the turn makes (a turn can make several when the
  model uses tools) into a new `TurnUsage` dataclass (`model_calls`,
  `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`).
- **Backward compatibility**: `run_turn` now returns `TurnMessages`, a
  `list[AnyMessage]` **subclass** carrying `.usage: TurnUsage` as an
  attribute — not a new return shape. Every existing caller that iterates,
  indexes, or spreads the return value (`evals/runner.py`,
  `integration/openai_adapter.py`, `services/webhook_worker.py`, the test
  suite) keeps working unmodified; only code that explicitly reads `.usage`
  sees the new data.
- **Honesty rule** (no invented numbers): if even one of a turn's model
  calls reports no `usage_metadata`, that turn's token totals are `None` —
  never a partial/guessed sum. A turn that times out reports
  `model_calls=None` (genuinely unknown), never a misleading `0`. The same
  rule is applied one level up when summing a run's turns and a scenario's
  runs (`evals/runner.py::_sum_turn_usage`).
- **Cost**: computed only from `Settings.model_prices` (env `MODEL_PRICES`),
  a `dict[model_id, ModelPrice(input_per_million, output_per_million)]`.
  `model_id` is the same id the caller already uses to select a runtime
  (the OpenAI adapter's `"{deployment}__{role}"` request model, or a
  live-eval's `model_name`). No entry for a model id → `cost_usd: None`,
  never a guessed rate.
- `POST /v1/chat/completions` reports the real `usage` numbers, `null` for
  any field that is honestly unknown (never coerced to `0`).
- One structured log event per turn, `runtime.turn_usage` (structlog, same
  logger as every other `runtime.*` event in `agent/graph.py`), carrying
  usage + cost. It picks up whatever `request_id` contextvar
  `RequestIdMiddleware` already bound for that request — no separate
  correlation-id plumbing was added.
- `evals/runner.py`: `RunOutcome.usage` (sum of a run's turns),
  `ScenarioResult.total_usage` (sum of a scenario's runs). `evals/reporting.py`
  reports both in the JSON (`total_tokens`, `total_cost_usd`, per-run
  `total_tokens`/`cost_usd`) and the markdown table (`Tokens`, `Cost (USD)`
  columns, `n/a` when unknown).

### Tasks
- [x] **S1.1 — `TurnUsage`/`TurnMessages` in `agent/graph.py`.** Added
  `AgentState.turn_usage` (reset per turn, same pattern as
  `tool_call_count`), `_call_model` records each call's `usage_metadata`,
  `run_turn` aggregates + logs + returns `TurnMessages`. RED: 6 new tests in
  `tests/test_agent_runtime.py` failed (`ImportError: cannot import name
  'TurnUsage'`/`'TurnMessages'`) against pre-implementation code. GREEN:
  `pytest -q tests/test_agent_runtime.py` 30 passed.
- [x] **S1.2 — `Settings.model_prices`.** Added `ModelPrice` + `model_prices`
  to `config.py`, documented (env `MODEL_PRICES`). RED: `tests/test_config.py`
  failed to collect (`ImportError: cannot import name 'ModelPrice'`) against
  pre-implementation code. GREEN: `pytest -q tests/test_config.py` 51 passed,
  2 xfailed.
- [x] **S1.3 — `/v1/chat/completions` real `usage`.** `openai_adapter.py`
  passes `model_id` to `run_turn` and reports `TurnMessages.usage` honestly
  (null fields when unknown; a runtime returning a plain list with no
  `.usage` is treated as "no usage reported"). RED: 3 tests in
  `tests/test_openai_adapter.py` failed against pre-implementation code
  (hardcoded zeros / missing `TurnMessages`/`model_id` forwarding). GREEN:
  `pytest -q tests/test_openai_adapter.py` 18 passed.
- [x] **S1.4 — eval report tokens/cost.** `evals/runner.py`
  (`RunOutcome.usage`, `ScenarioResult.total_usage`, `_sum_turn_usage`) and
  `evals/reporting.py` (JSON + markdown columns). RED: 2 failures in
  `tests/test_eval_runner.py` + 3 failures in `tests/test_eval_reporting.py`
  against pre-implementation code. GREEN: `pytest -q tests/test_eval_runner.py
  tests/test_eval_reporting.py` 38 passed.
- [x] **S1.5 — docs.** `docs/platform/live-eval.md` and its `_es` twin: new
  "Tokens and cost" section (honesty rule, `MODEL_PRICES` shape, where it
  shows up in the report).
- [x] **S1.6 — full verification.** `ruff check .` all checks passed;
  `ruff format --check .` 365 files already formatted (after one
  `ruff format .` fix-up for 2 files); `mypy src/` success, 69 source files;
  `pytest -q` 1405 passed, 97 deselected, 17 xfailed. RED evidence for the
  whole slice: `git stash` of only the 6 implementation files (tests kept)
  reproduced 14 failures + 1 collection error across
  `test_agent_runtime.py`/`test_config.py`/`test_eval_runner.py`/
  `test_eval_reporting.py`/`test_openai_adapter.py`; `git stash pop`
  restored GREEN.
- [x] **S1.7 — commit + PR.** Work-unit commit: `e20cc99` ("feat(observability):
  real token usage and cost per turn (#78 1/3)"). PR:
  `feat(observability): real token usage and cost per turn (#78 1/3)`,
  `Refs #78` (not `Closes` — two slices remain).

## Slice 2 — process memory/CPU, durations, `/metrics` (pending)
Not started. Per the issue: process memory/CPU sampling (no `psutil`/
`resource.getrusage` anywhere in `src/` today), turn + per-tool-call
duration, exposed via `/metrics` or folded into `GET /health`
(`main.py:health()`).

## Slice 3 — real-app live harness (pending)
Not started. A harness that boots the actual application (HTTP API, WhatsApp
webhook, Postgres, Redis, audit-to-DB) instead of calling
`AgentRuntime.run_turn` directly the way `evals/runner.py:run_scenario` does
today. Phase 1 (#79) and Phase 2 (extends #76) run on top of this harness.

## Rollback
Slice 1 is additive: `TurnMessages`/`TurnUsage`/`model_prices` are new, and
`run_turn`'s extra `model_id` parameter is optional with a `None` default.
Reverting the commit restores the previous hardcoded-zero `usage` response
and the previous `list[AnyMessage]` return value; no migration, no data
change.
