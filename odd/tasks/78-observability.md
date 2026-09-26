# #78 — Live-test Phase 0 — real observability

## Objective
Prove, with real numbers, what a turn against a real LLM costs and how it
performs, per `docs/delivery/live-test-plan.md`'s Phase 0 and Principle
("every assertion targets what the system did"). Phase 0 is delivered in
three slices; this document tracks all of them so a later slice resumes with
full context.

## Scope (this document covers all of #78; Slices 1 and 2 are implemented)
- Slice 1 (done): real token usage and cost per turn.
- Slice 2 (this work unit, done): process memory/CPU sampling, turn/tool
  durations, and exposing all of it via `GET /metrics`.
- Slice 3 (pending): the real-app live harness (API, webhook, Postgres,
  Redis, audit-to-DB) that later phases (#79, #76-extends) build on.

Out of scope for Slices 1/2 (tracked as Slice 3 above): the real-app live
harness. Out of scope for Slice 2 specifically (per its own PR description):
alerts/OpenTelemetry (ADR-005 section 6/7 — this slice unblocks them, does
not build them), and per-tool-call durations in the live-eval report
(available instead via `/metrics`'s histogram — see Slice 2's Design below).

## TDD mode
Strict TDD (per `.pi/ops/prompts/common-rules.txt` item 10 and the repo's
global CLAUDE.md). Runner: `pytest -q` from the worktree root with
`PATH=<repo>/.venv/bin:$PATH PYTHONPATH=<worktree>/src`.

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
- [x] **S1.8 — review fixes (PR #87 adversarial review, `.pi/ops/logs/review87.txt`).**
  Strict TDD per finding, each reproduced with its probe
  (`.pi/ops/logs/review-87-probes/`) before the fix:
  - **Finding 1 (CRITICAL, OpenAI SDK compat).** `_usage_payload`
    (`integration/openai_adapter.py`) now returns `None` (top-level
    `"usage": null`) for the WHOLE object whenever any field is unknown,
    never an object with `null` fields — the official `openai` SDK's
    `CompletionUsage` requires non-Optional ints. Added a test validating
    both shapes against the real SDK
    (`openai.types.chat.ChatCompletion.model_validate`).
  - **Finding 2 (HIGH, scenario-total honesty).** `ScenarioResult.total_usage`
    (`evals/runner.py`) is now unknown as soon as ANY run's usage is unknown
    (including a crashed run), never a partial sum across only the runs that
    succeeded. New test with `runs=3` (one crashed) — the previous
    `runs=1` test could not expose this gap.
  - **Finding 3 (HIGH, retries under-report).** `_call_model`'s
    tool-format-error retry now records the failed first attempt as an
    explicit `None` usage entry (2 real calls counted, tokens honestly
    null) instead of silently dropping it.
    `_ainvoke_with_optional_checkpointer`'s same-turn degradation retry now
    returns a `turn_usage=None` sentinel when degradation occurred (the
    failed attempt's tokens are unrecoverable — LangGraph's `ainvoke` raises
    with no partial state — so the WHOLE turn is reported fully unknown,
    the same honest choice AD-3's timeout backstop already makes, rather
    than a plausible-looking undercount from only the retried invocation).
  - **Finding 4 (MEDIUM, price keying).** `Settings.model_prices` is now
    keyed by the PROVIDER MODEL id, not a caller-chosen routing id.
    `model_display_name` moved from `evals/provider.py` to
    `agent/graph.py` (re-exported from its old location for backward
    compatibility); `AgentRuntime.__init__` derives `self._model_id` from
    it at construction time. `run_turn(_with_usage)`'s `model_id` argument
    is now at most an optional override. `openai_adapter.py` no longer
    passes its own request model id as that override (it was the bug —
    pricing config had to be duplicated per runtime id and never matched
    what the eval pipeline used); `services/webhook_worker.py` needed no
    code change (it never passed one) but is now priced correctly for free.
  - **Finding 5 (MEDIUM, `TurnMessages` drops `.usage`).** Replaced the
    `list[AnyMessage]` subclass with an explicit `TurnResult(messages,
    usage)` frozen dataclass. `run_turn` keeps returning
    `list[AnyMessage]` (delegates to the new method, returns
    `.messages`) — every existing caller that never touched `.usage` keeps
    working unmodified. `run_turn_with_usage` is the new explicit entry
    point; `evals/runner.py::run_scenario` and
    `integration/openai_adapter.py::chat_completions` were updated to call
    it instead of reading `.usage` off a plain list.
  - **Finding 6 (LOW, `ModelPrice` validation gaps).** Added
    `extra="forbid"`, `allow_inf_nan=False`, and a finite upper bound
    (10,000 USD/M tokens) to both price fields.
  - **Finding 7 (LOW, missing tests).** Added: a 2-turn checkpointed thread
    does not double-count turn 1 into turn 2; `_limit_reached`'s own fixed
    message adds no extra `model_calls` entry (while the two REAL
    `call_model` calls the breach scenario legitimately makes are both
    counted); both retry paths (finding 3, above).
  Docs updated (EN + this file's own update): `docs/platform/live-eval.md`
  and its `_es` twin — the provider-model-id key format, the
  derive-then-override default, and the `usage: null` object-level rule.
  Full verification: `ruff check .` all checks passed; `ruff format --check .`
  366 files already formatted; `mypy src/` success, 69 source files;
  `pytest -q` 1417 passed, 97 deselected, 17 xfailed. RED evidence for the
  whole slice: restoring the 6 pre-fix implementation files (`agent/graph.py`,
  `config.py`, `evals/provider.py`, `evals/runner.py`,
  `integration/openai_adapter.py`, `services/webhook_worker.py`) over the
  updated tests reproduced 31 failures across `test_agent_runtime.py` (11),
  `test_config.py` (3), `test_eval_runner.py` (2), and
  `test_openai_adapter.py` (15, several via `ImportError: cannot import
  name 'TurnResult'`); restoring the fixed implementation files returned to
  GREEN (same full-suite result above).
- [x] **S1.9 — second review round (PR #87), overflow in cost computation.**
  A hostile or compromised `adapter_provider="openai_compatible"` backend
  (an explicitly supported, operator-selectable third-party/self-hosted
  endpoint) can return a syntactically valid but absurdly large
  `prompt_tokens`/`completion_tokens` value -- JSON integers have no size
  ceiling and `UsageMetadata` does not bound them -- which raised an
  unhandled `OverflowError` out of `_compute_turn_cost`, failing every turn
  for any deployment with a configured price for that model (each caller's
  generic `except Exception` still failed closed to a 500/retry, so this was
  a reliability/DoS gap, not data corruption). Reproduced with
  `.pi/ops/logs/review-probes/pr87-fix/probe_overflow_cost.py`. Fixed by
  bounding `_compute_turn_cost`'s inputs to a plausible per-turn range
  (`_MAX_PLAUSIBLE_TURN_TOKENS = 100_000_000`, negative rejected too) and
  returning `None` instead of computing -- the same honesty-null contract
  as a missing price, never a crash. Three new tests in
  `test_agent_runtime.py` (huge value, negative value, ordinary value
  unaffected); RED observed via `pytest -k compute_turn_cost` before the
  fix (`OverflowError` / a wrong non-`None` cost for the negative case).
  Docs updated (EN + ES, `docs/platform/live-eval.md`) with the plausible
  range. Full re-verification: `ruff check .` all checks passed;
  `ruff format --check .` 366 files already formatted; `mypy src/` success,
  69 source files; `pytest -q` 1421 passed, 97 deselected, 17 xfailed.

## Slice 2 — process memory/CPU, durations, `GET /metrics`

### Design
- New `src/agents_system/observability/metrics.py` (new direct dependency:
  `prometheus-client`, confirmed clean by the repo's `dependency-audit` CI
  job / a local `pip-audit --local` run). Every metric is built against an
  explicit `CollectorRegistry` via `build_metrics(registry)` — never bound
  to `prometheus_client`'s own ambient default registry — so a test builds
  its own throw-away set (`build_metrics(CollectorRegistry())`) with no
  collision risk. `DEFAULT_METRICS`/`DEFAULT_REGISTRY` are the process-wide
  set built once at import time; `build_metrics` also registers the
  default `ProcessCollector` on the same registry (resident memory + CPU
  seconds on Linux, confirmed by a runtime test, not assumed).
- Counters/histograms (`agent_turns_total`, `agent_turn_duration_seconds`,
  `agent_tool_calls_total`, `agent_tool_call_duration_seconds`,
  `agent_limit_trips_total`, `agent_tokens_total`, `agent_cost_usd_total`),
  recorded at the agent graph's existing choke points
  (`agent/graph.py::_finish_turn` for turns, `_execute_tools` for tool
  calls, `_limit_reached` for limit trips) — never scattered elsewhere.
  Labels are bounded, code-defined enums or operator-configured ids only
  (`runtime_id`, `outcome`, `tool`, `limit`, `direction`) — never a user
  id, phone number, correlation id, or message text; locked down by tests
  that pin the exact label schema and drive a real turn with a
  phone-number-shaped `session_id`, asserting it never reaches the
  exported text.
- `AgentRuntime.__init__` gained two backward-compatible optional
  parameters: `runtime_id` (defaults to this runtime's own derived
  provider model id, same default `run_turn`'s `model_id` pricing override
  already uses) and `metrics` (defaults to the process-wide
  `DEFAULT_METRICS`). `main.py`'s lifespan passes `runtime_id=model_id`
  (the operator's own `"{deployment}__{role}"` id) explicitly; every other
  existing construction site is unaffected.
- Turn/tool-call outcomes: turns are `ok` (includes a turn that legitimately
  ended at `_limit_reached` — that is a normal completion, not a failure),
  `timeout` (`total_execution_timeout_s`), or `error` (anything else —
  recorded via a new outer `except Exception: record; raise` wrapper around
  `run_turn_with_usage`'s existing timeout handling, which changes no
  existing error-handling behavior, only adds observability). Tool calls
  are `ok`, `denied` (`PolicyViolation(reason="not_in_surface")` — a tool
  never equipped at all), `blocked` (`reason` in
  `{"revalidation_required", "permission_revoked"}` — Layer-2 revalidation
  failure on a tool that WAS equipped), `timeout`
  (`tool_call_timeout_s`), or `error` (the connector itself raises,
  likewise recorded-then-re-raised via a new `except Exception` branch).
- `GET /metrics` (`main.py`, Prometheus text via `generate_latest`): off by
  default (`Settings.metrics_enabled=False` → 404, absent not merely
  unauthenticated), Bearer-protected once enabled
  (`Settings.metrics_api_key`, `secrets.compare_digest`, exactly
  `integration/openai_adapter.py::verify_bearer`'s fail-closed shape), open
  only under the existing `ALLOW_INSECURE=true` escape hatch — enforced by
  `Settings.validate_security_fail_closed` (extended, not replaced) and a
  new `metrics.open_mode` startup warning mirroring `adapter.open_mode`.
- Live-eval report (`evals/runner.py`/`reporting.py`): `RunOutcome.duration_s`
  (real wall-clock sum of a run's turns, `time.monotonic()` around each
  `run_turn_with_usage` call) and `ScenarioResult.total_duration_s`
  (summed across runs, `None` as soon as any one run's duration is unknown
  — same honesty-rule shape as `total_usage`, though duration itself is
  never "partially unknown" the way a provider-reported token count can
  be: it is `None` only for a run that crashed before its first turn
  returned). `write_results` reports it in both outputs (`total_duration_s`
  JSON field, `Duration (s)` markdown column). Per-tool-call durations were
  deliberately left out of this report (documented "if cheap" scope
  decision — would need `TurnResult` to also carry a duration list); use
  `/metrics`'s `agent_tool_call_duration_seconds` histogram instead.
- Docs: new `docs/platform/observability.md` + `_es` twin (full metric/label
  reference, protection model, testing pattern); `docs/platform/live-eval.md`
  + `_es` twin gained a "Turn duration" section; ADR-005 section 6 (EN+ES)
  updated to reflect the counters now existing.

### Tasks
- [x] **S2.1 — `observability/metrics.py`.** `Metrics`/`build_metrics`/
  `record_turn`/`record_tool_call`/`record_limit_trip`, `DEFAULT_METRICS`/
  `DEFAULT_REGISTRY`. RED: `tests/test_observability_metrics.py` failed
  collection (`ModuleNotFoundError`) against pre-implementation code.
  GREEN: `pytest -q tests/test_observability_metrics.py` 8 passed.
- [x] **S2.2 — `agent/graph.py` choke points.** `_execute_tools`,
  `_limit_reached`, `_build_graph`, `AgentRuntime.__init__`/
  `run_turn_with_usage`/`_finish_turn`. RED: 9 new tests in
  `tests/test_agent_runtime.py` failed (`TypeError`/`AttributeError`)
  against pre-implementation code (verified by restoring the pre-slice-2
  file over the updated tests); the pre-existing 39 tests in that file
  were unaffected throughout. GREEN: `pytest -q tests/test_agent_runtime.py`
  48 passed.
- [x] **S2.3 — `GET /metrics` + `Settings`.** `config.py`
  (`metrics_enabled`/`metrics_api_key`, `validate_security_fail_closed`
  extended), `main.py` (`verify_metrics_access`, the route, the startup
  warning, `runtime_id=model_id` at the `AgentRuntime` construction site).
  RED: 4 new tests in `tests/test_config.py` + 4 new tests in
  `tests/test_metrics_endpoint.py` failed against pre-implementation code.
  GREEN: `pytest -q tests/test_config.py tests/test_metrics_endpoint.py
  tests/test_main.py tests/test_openai_adapter.py` all passed.
- [x] **S2.4 — eval report duration.** `evals/runner.py`
  (`RunOutcome.duration_s`, `ScenarioResult.total_duration_s`),
  `evals/reporting.py` (JSON field + markdown column). RED: 2 failures in
  `tests/test_eval_runner.py` + 1 failure in `tests/test_eval_reporting.py`
  against pre-implementation code. GREEN: `pytest -q tests/test_eval_runner.py
  tests/test_eval_reporting.py tests/test_live_eval_roles.py
  tests/test_live_eval_sales_agent.py` 44 passed.
- [x] **S2.5 — docs.** `docs/platform/observability.md` + `_es` twin (new),
  `docs/platform/live-eval.md` + `_es` twin ("Turn duration" section +
  cross-reference), `docs/architecture/adr-005-operational-safety-roadmap.md`
  + `_es` twin (section 6 updated).
- [x] **S2.6 — full verification.** Aggregate RED evidence across every
  implementation file at once (`observability/{__init__,metrics}.py`,
  `agent/graph.py`, `config.py`, `main.py`, `evals/{runner,reporting}.py`
  restored to pre-slice-2 content, updated tests kept): 22 failures +
  1 collection error (8 more tests) across `test_agent_runtime.py`,
  `test_eval_reporting.py`, `test_eval_runner.py`, `test_config.py`,
  `test_metrics_endpoint.py`, `test_observability_metrics.py`; the 203
  pre-existing tests in that same file set stayed green throughout,
  confirming no regression. Restoring the implementation returned to
  GREEN. Full-suite result: `ruff check .` all checks passed; `ruff format
  --check .` 383 files already formatted; `mypy src/` success, 71 source
  files; `pytest -q` 1562 passed, 101 deselected, 17 xfailed.
- [x] **S2.7 — commit + PR.** Work-unit commit: `24157a1`
  ("feat(observability): /metrics with process, turn and tool metrics
  (#78 2/3)"). PR: https://github.com/nahuel893/agents-system/pull/96,
  `Refs #78` (not `Closes` — Slice 3 remains). Not merged.
  **CI**: all 12 checks green (`ci`, `secret-scan`, `dependency-audit`,
  `shellcheck`, and every integration job) after 4 small follow-up
  commits (`b0095db` records this commit id in this doc; `2464eb7`,
  `3509346`, `5268393`, `d2e3d45` fix a `secret-scan` (gitleaks)
  false positive on the `GET /metrics` curl example's placeholder
  token — see `.gitleaksignore`'s new entries for the full triage
  trail). No production code changed by any of these four; all are
  docs/`.gitleaksignore` only. (These commit ids were later rewritten by
  the S2.8 rebase; see S2.8 for the post-rebase ids.)
- [x] **S2.8 — review fix: denied-tool-call label leak.** A review of
  PR #96 found that `agent_tool_calls_total{tool=...}` recorded the
  model's own raw tool name for a `denied` (`not_in_surface`) tool
  call — untrusted, model-supplied text (prompt-injected or
  hallucinated) that had never been checked against the equipped
  surface, so it could carry PII or grow the label's cardinality
  without bound.
  - RED: reverted the eventual fix in the working tree and re-ran
    `test_tool_call_to_unknown_tool_records_denied_outcome` plus a new
    `test_denied_tool_call_never_leaks_attacker_tool_name` (attacker
    payload: a phone number plus Prometheus exposition-format
    metacharacters) — both failed (`assert 0.0 == 1` on the
    `_unrecognized_` label).
  - GREEN: fix commit `ad2f0d8` (`fix(observability): never label
    agent_tool_calls_total with a denied tool's raw name`) —
    `_execute_tools` now substitutes the fixed placeholder
    `_unrecognized_` for that one outcome only; every other outcome
    keeps its already-equipped tool name. Docs updated (EN+ES). Both
    tests pass; full offline suite `pytest -q` 1666 passed, 101
    deselected, 17 xfailed; `ruff check`, `ruff format --check`,
    `mypy src/` all clean.
  - The fix was applied by rebasing the branch onto the then-current
    `origin/main` (`ca827bd`, PR #95 having merged meanwhile), which
    rewrote every prior commit's id — see the branch log for the
    current ids of the S2.1–S2.7 commits.
  - Follow-up commit `b111630` (`chore(security): refresh
    gitleaksignore fingerprints after PR #96 rebase`) — the rebase
    changed the commit SHAs the three `.gitleaksignore` entries from
    S2.7 keyed on, so `secret-scan` re-flagged the same
    already-triaged documentation-placeholder finding under the new
    SHAs; only the SHAs were refreshed, same reasoning as before.
  - PR #96 all 12 CI checks green again after both commits. Not
    merged.

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

Slice 2 is additive too: `observability/metrics.py` is new;
`AgentRuntime`'s `runtime_id`/`metrics` parameters are optional with
backward-compatible defaults; `GET /metrics` is a new route, off by default
(`metrics_enabled=False`); `RunOutcome.duration_s`/
`ScenarioResult.total_duration_s` are new fields with `None`/computed
defaults. Reverting the commit removes the endpoint and the counters; no
migration, no data change, no existing behavior altered (verified by the
aggregate RED/GREEN check in S2.6, which pinned the 203 pre-existing tests
in the same files as unaffected).
