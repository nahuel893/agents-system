# Observability — process, turn and tool metrics

Issue #78 Phase 0 Slice 2. Real numbers for what the running application
costs and how it performs: process resident memory and CPU, turn and
tool-call durations, and a `GET /metrics` endpoint in Prometheus text
format — the "minimal counter set (tool calls, denials, limit trips)"
ADR-005 section 6 (`docs/architecture/adr-005-operational-safety-roadmap.md`)
calls for before any broader OpenTelemetry rollout. Slice 1 (real per-turn
token usage and cost) is documented in `docs/platform/live-eval.md`'s
"Tokens and cost" section; this page covers everything else Phase 0 adds.

## `GET /metrics`

Prometheus text exposition (`prometheus_client.generate_latest`) of the
process-wide metrics registry. Off by default and absent (404), not merely
unauthenticated, until an operator explicitly turns it on:

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `metrics_enabled` | `METRICS_ENABLED` | `False` | `GET /metrics` returns 404 when unset. |
| `metrics_api_key` | `METRICS_API_KEY` | `""` | Bearer token required once `metrics_enabled` is set. |

```bash
export METRICS_ENABLED=true
export METRICS_API_KEY=some-long-random-token
curl -H "Authorization: Bearer some-long-random-token" http://localhost:8000/metrics
```

**Protection model** follows `integration/openai_adapter.py`'s
`ADAPTER_API_KEY`/`ALLOW_INSECURE` fail-closed spirit exactly
(`main.py::verify_metrics_access`, `Settings.validate_security_fail_closed`):

- `metrics_enabled=False` (default): the route exists in the app, but the
  dependency raises 404 before doing anything else — the same posture as
  the endpoint not being registered at all.
- `metrics_enabled=True` + `metrics_api_key` set: `Authorization: Bearer
  <key>` is required, checked with `secrets.compare_digest` (constant-time,
  same mechanism as the OpenAI adapter's `verify_bearer`). Missing or wrong
  token → 401.
- `metrics_enabled=True` + `metrics_api_key` empty: `GET /metrics` is open
  to anyone who can reach the process. `Settings.validate_security_fail_closed`
  refuses to boot this combination unless `ALLOW_INSECURE=true` is also set
  explicitly — the same dev-only escape hatch the adapter's own open mode
  uses. A boot under this combination also logs one `metrics.open_mode`
  warning.

## What is measured

Recorded at the agent loop's existing choke points
(`src/agents_system/agent/graph.py`) — `AgentRuntime.run_turn_with_usage`
(via its shared `_finish_turn` return path), `_execute_tools`, and
`_limit_reached` — never scattered across additional call sites. All
metric objects live in `src/agents_system/observability/metrics.py`.

| Metric | Type | Labels | Recorded when |
|---|---|---|---|
| `agent_turns_total` | Counter | `runtime_id`, `outcome` | Every turn completes: `outcome` is `ok`, `timeout`, or `error`. |
| `agent_turn_duration_seconds` | Histogram | `runtime_id`, `outcome` | Same as above — wall-clock duration of the whole `run_turn_with_usage` call. |
| `agent_tokens_total` | Counter | `runtime_id`, `direction` (`input`/`output`) | A turn's usage is known (see the honesty rule below). |
| `agent_cost_usd_total` | Counter | `runtime_id` | A turn's `cost_usd` is known (same `Settings.model_prices` lookup as slice 1). |
| `agent_tool_calls_total` | Counter | `tool`, `outcome` | Every tool call attempt: `outcome` is `ok`, `denied`, `blocked`, `timeout`, or `error`. |
| `agent_tool_call_duration_seconds` | Histogram | `tool`, `outcome` | Same as above. |
| `agent_limit_trips_total` | Counter | `limit` | An execution limit fires — today only `"max_tool_calls"` (the `_limit_reached` terminal node; ADR-005 section 6's "limit trips"). |
| `process_resident_memory_bytes`, `process_cpu_seconds_total`, and the rest of `prometheus_client`'s default `ProcessCollector` | Gauge/Counter | none | Always, for the running process — confirmed present on Linux by `tests/test_observability_metrics.py`, not merely assumed from the library's docs. |

**Turn outcomes**: `ok` also covers a turn that legitimately ended at the
`_limit_reached` node — that is a normal completion, not a failure; the
limit firing is its own `agent_limit_trips_total` sample. `timeout` is
`total_execution_timeout_s` firing. `error` is anything else the turn
raises (a genuine provider failure, an unexpected checkpointer error) —
recorded, then re-raised unchanged, so this metric adds observability
without changing any existing error-handling behavior.

**Tool-call outcomes**: `denied` is a `PolicyViolation` for a tool that was
never in the equipped surface at all (`reason="not_in_surface"` — Layer-1
territory, e.g. a hallucinated tool name). `blocked` is a Layer-2
revalidation failure on a tool that WAS equipped
(`reason` in `{"revalidation_required", "permission_revoked"}` —
`harness/interceptor.py`). `timeout` is `tool_call_timeout_s` firing for
that one call. `error` is anything else the connector itself raises —
recorded, then re-raised unchanged, same as the turn-level `error` outcome.

**Honesty rule for tokens/cost** — same contract as slice 1's `TurnUsage`:
`agent_tokens_total`/`agent_cost_usd_total` are only incremented when the
value is actually known. A timed-out or errored turn increments
`agent_turns_total`/`agent_turn_duration_seconds` but never a guessed `0`
token or cost sample.

## Label hygiene

Labels are bounded, code-defined enums or operator-configured identifiers —
**never** a user id, phone number, correlation/request id, or message text.
Concretely: `runtime_id` (see below), `outcome`/`limit`/`direction` (fixed
string enums), `tool` (a connector name from the finite, operator-injected
`ToolRegistry`). There is no parameter on `record_turn`/`record_tool_call`/
`record_limit_trip` through which an unbounded value could reach a label —
`tests/test_observability_metrics.py` and `tests/test_agent_runtime.py`
both lock this down: one test pins the exact label-name schema, another
drives a real turn with a phone-number-shaped `session_id` and asserts it
never appears anywhere in the exported text (`session_id` is never a label
at all — it is not even a parameter of any recording function).

## `runtime_id`

`AgentRuntime.__init__` accepts an optional `runtime_id: str | None`.
`None` (the default) falls back to this runtime's own derived provider
model id (`model_display_name(model)` — the same default slice 1's
`model_id` pricing override already uses), so every existing construction
site (`evals/runner.py`, direct library use via `agents_system.AgentRuntime`)
is unaffected. `main.py`'s lifespan — the only caller with a more
meaningful id on hand — passes the operator's own
`"{deployment}__{role}"` runtime id explicitly, so a real deployment's
metrics are labeled by the id an operator actually configured, not by
which underlying model happens to be serving it.

## Testing pattern

Every metric is built against an explicit `prometheus_client.CollectorRegistry`
via `observability.metrics.build_metrics(registry)` — never bound to
`prometheus_client`'s own ambient default registry. `DEFAULT_METRICS`/
`DEFAULT_REGISTRY` are the process-wide set `GET /metrics` serves, built
once at import time. A test builds its own throw-away set —
`build_metrics(CollectorRegistry())` — so tests never collide with each
other (registering the same metric name twice on one registry raises
`ValueError`) or mutate process-wide state another test could observe. Pass
a fresh set into `AgentRuntime(..., metrics=my_metrics)` to assert on
exactly the values one test's turns produced.

## Out of scope for this slice

- The real-app live harness (issue #78 Slice 3).
- Alerts, SLOs, and push-based notification (ADR-005 section 7 —
  unblocked by this slice's counters, not built by it).
- OpenTelemetry / distributed tracing (ADR-005 section 6's own "broader
  rollout", explicitly deferred until after this minimal counter set).
- Per-tool-call durations in the live-eval report (`evals/reporting.py`):
  only turn duration was added there (see `docs/platform/live-eval.md`) —
  getting per-tool-call durations into that report cheaply would need
  `TurnResult` to also carry a list of tool-call durations, which this
  slice did not add. Use `agent_tool_call_duration_seconds` via `/metrics`
  for that instead.

## Cross-references

- ADR-005 section 6 (Observability) / section 7 (Monitoring and alerts):
  `docs/architecture/adr-005-operational-safety-roadmap.md`
- Tokens and cost (issue #78 Slice 1): `docs/platform/live-eval.md`
- Metrics code: `src/agents_system/observability/metrics.py`
- Recording call sites: `src/agents_system/agent/graph.py`
  (`_execute_tools`, `_limit_reached`, `_finish_turn`)
- `GET /metrics` route + `verify_metrics_access`: `src/agents_system/main.py`
- Offline tests: `tests/test_observability_metrics.py`,
  `tests/test_metrics_endpoint.py`, `tests/test_agent_runtime.py`
  (metrics section), `tests/test_config.py` (fail-closed validator)
