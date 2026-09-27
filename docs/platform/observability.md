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
curl -H "Authorization: Bearer $METRICS_API_KEY" http://localhost:8000/metrics
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

## Health and readiness

`GET /health` is a **liveness** endpoint: it always returns HTTP 200 and its
body's `status` is `ok` or `degraded`. It reports PostgreSQL, Redis, webhook
outbox state, the chat provider used by configured runtimes, and audit loss.
When no runtime is configured, the provider status is `not_configured` and no
provider request is made. Otherwise, the probe is a credential-free `HEAD`
request to its base URL, bounded by a one-second total timeout and cached for
five seconds per configured provider target. Concurrent cache misses share one
in-flight request, preventing upstream probe bursts. Any HTTP response
(including authentication or route errors) proves reachability; a transport or
timeout failure makes `provider.status` `error` with the fixed
`"unreachable"` reason. It never sends tokens, credentials, or a provider
error message in the response.

`GET /ready` returns the same body but is the **readiness** endpoint: it is
200 only when the body's status is `ok`, otherwise 503. Configure an
orchestrator's liveness probe to use `/health` and its readiness/traffic gate
to use `/ready`.

The `audit.dropped_count` health field is the process-local total of audit
events the asynchronous sink could not persist. No outbox silent-loss counter
exists: outbox work is durable and terminal failures remain visible as
backlog/worker state instead of being dropped.

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
| `agent_audit_events_dropped` | Gauge | none | Current process-local `AuditSink.dropped_count`, sampled when `GET /metrics` is served. |
| `agent_provider_requests_total` | Counter | `provider`, `status` | Every raw HTTP attempt the chat model's own async client makes (issue #145) — see "Provider observability, and the issue #145 investigation" below. |
| `agent_provider_retries_total` | Counter | `provider` | Every attempt the provider SDK's OWN retry loop re-sends after an earlier attempt on the same call failed (issue #145). |
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
territory, e.g. a hallucinated or prompt-injected tool name). Because that
name was never checked against the equipped surface, it is untrusted,
model-supplied text — the `tool` label for a `denied` sample is always the
fixed placeholder `_unrecognized_`, never the model's own tool name (see
"Label hygiene" below). `blocked` is a Layer-2 revalidation failure on a
tool that WAS equipped (`reason` in
`{"revalidation_required", "permission_revoked"}` —
`harness/interceptor.py`); its `tool` label is that equipped tool's real
name. `timeout` is `tool_call_timeout_s` firing for that one call. `error`
is anything else the connector itself raises — recorded, then re-raised
unchanged, same as the turn-level `error` outcome.

**Honesty rule for tokens/cost** — same contract as slice 1's `TurnUsage`:
`agent_tokens_total`/`agent_cost_usd_total` are only incremented when the
value is actually known. A timed-out or errored turn increments
`agent_turns_total`/`agent_turn_duration_seconds` but never a guessed `0`
token or cost sample.

## Provider observability, and the issue #145 investigation

Issue #145 ("under 20 concurrent conversations with a real model, only 3/20
finish within 240s") suspected chat-model-provider throttling: the reported
symptom — most turns failing, "no error ever returned" — is exactly what a
429 silently absorbed by the provider SDK's own retry loop would look like,
since `agent_turns_total{outcome="error"}` alone cannot say *why* a turn
failed. `MetricsHTTPTransport`
(`src/agents_system/observability/provider_http.py`) was built to make that
visible: it wraps the `openai_compatible` chat model's async httpx client so
every raw HTTP attempt increments `agent_provider_requests_total`, labeled by
`provider` (a bounded, code-defined value — `openai_compatible` today) and
`status` — one of `success`, `rate_limited` (a raw 429), `client_error`,
`server_error`, or `network_error` (a connect/read/write timeout or other
transport failure), classified by `classify_provider_status`. Every attempt
the SDK's own retry loop re-sends (recognized via the
`x-stainless-retry-count` header every Stainless-generated SDK — openai,
groq, anthropic — stamps on retried requests) also increments
`agent_provider_retries_total{provider}` — a retry that never raises is
otherwise invisible.

**That instrumentation is also what disproved the hypothesis.** Reproduced
against a real `EVAL_PROVIDER=openai_compatible` account at 5, 10 and 20
concurrent real-model turns through the full webhook → outbox →
`AgentRuntime.run_turn` path, BEFORE the fix below: a single fresh
concurrency=20 confirmation run completed 10/20 conversations in 240s (p50
84.96s, p95 158.20s), with `agent_provider_requests_total{
provider="openai_compatible", status="success"}` = 73 for 73 raw HTTP
attempts — every single request to the provider succeeded. Zero
`rate_limited`, `client_error`, `server_error`, or `network_error` samples,
at any concurrency across the whole investigation. The provider was never
the bottleneck.

Cross-referencing `agent_turns_total{outcome="error"}` against
`agent_tool_calls_total{outcome="error",tool="catalog_search"}` for the SAME
runs found an exact 1:1 match every time (12/12, 63/63, 126/126 across the
three rounds) — every failed turn had failed inside the `catalog_search`
tool call, never inside the model call itself. The cause: `sales-agent`'s
`catalog_search` queries a demo `articulos` table that
`evals.live_harness.boot_live_harness` only creates when called with
`seed_demo=True` (`docs/platform/reference-backends.md`) — and the Phase 3
real-model scenario (`tests/test_live_load_phase3.py`, from #83/PR #144, the
scenario issue #145 itself cites) never passed it, even though its own
prompt ("Do you have Item Alpha in stock?") is designed to trigger that tool.
Every real call to `catalog_search` raised
`asyncpg.exceptions.UndefinedTableError: relation "articulos" does not
exist`, an unhandled exception that failed the whole turn and fed
`services/outbox.py`'s retry-with-backoff loop (`MAX_OUTBOX_ATTEMPTS=5`,
`_RETRY_BASE_SECONDS * 2**attempt`) — which never sends the customer a
reply while retrying, matching the "no error ever returned" symptom exactly.
The load suite's OTHER benchmark (100 concurrent conversations with a
deterministic fake model, `agent_turn_duration_seconds` p95 13.8s, no
errors) never exercises this at all: that fake model is hardcoded to never
call a tool (`tests/test_live_load_phase3.py::_LoadTestFakeModel`'s own
docstring), so it structurally cannot hit `catalog_search`'s missing-schema
bug — explaining why the fast/clean 100-concurrent number and the slow/broken
20-real-model number were never actually comparable.

**The fix has two parts, both "our side," not the provider's:**

1. `tests/test_live_load_phase3.py`'s real-model scenario now passes
   `seed_demo=True`, matching every other live-eval scenario that touches
   `catalog_search`/`client_lookup`/`run_report`
   (`tests/test_live_eval_phase1.py`) — so this benchmark actually measures
   a real model's real latency and capacity, not a demo-data setup bug.
2. `services/reference.py`'s `catalog_search`/`client_lookup` (and, through
   it, `message_sender`'s own recipient check) now catch a
   `sqlalchemy.exc.SQLAlchemyError` from the query itself and degrade to the
   SAME `error_kind: "reference_data_not_configured"` response an unset
   `readonly_engine` already returns, logging one
   `reference_backends.demo_schema_unavailable` warning — instead of letting
   a missing/misconfigured demo schema crash the whole turn. This is defense
   in depth for a real deployment that reuses this reference backend without
   having actually migrated/seeded its schema; see
   `docs/platform/reference-backends.md`'s "Observable demo behavior".

**What stays useful even though the provider was not at fault this time:**
the per-call HTTP status/retry counters give real 429/5xx/timeout visibility
the platform never had before, and the bounded per-call timeout below closes
a genuine gap (an unbounded 600s default) that a future, actually-throttled
account WOULD hit. Re-run after both fixes, same 20-concurrency scenario:
**20/20 conversations completed in 76.92s** (min 18.88s, p50 59.28s, p95
76.67s, max 76.91s — real-model latency, now free of the schema crash),
`agent_turns_total{outcome="ok"}` = 20 with zero `error`/`timeout` samples,
`agent_tool_calls_total{outcome="ok",tool="catalog_search"}` = 78 with zero
errors, and `agent_provider_requests_total{provider="openai_compatible",
status="success"}` = 96 — every raw HTTP attempt made, none rate-limited or
failed. No `outbox_work.last_error` rows at all. Compare the ORIGINAL
issue's own numbers (3/20 within 240s, p50 95s, p95/max 152s for the three
that completed) and this PR's own before-fix reproduction just above (10/20,
p50 84.96s, p95 158.20s, 63 tool-call errors) — the fix is not a latency
optimization, it is removing an unhandled exception that was silently
discarding most conversations into the outbox's retry loop.

**Bounded per-call timeout.** The `openai_compatible` chat model previously
left `request_timeout` unset, which put the openai SDK's own default in
effect: 5s to connect, but 600s (ten minutes) for the response itself. One
stuck call could silently hold a turn for up to ten minutes before any
platform-level bound (`total_execution_timeout_s`, 60s by default) had a
chance to matter in practice for that call's own retries/backoff. `Settings.
openai_compatible_request_timeout_s` (env `OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S`,
default `45.0`) now bounds it explicitly, comfortably under
`total_execution_timeout_s` — a single hung provider call now fails with a
clear, typed timeout instead of waiting silently.

**Account sizing guidance for operators.** `max_concurrent_turns` (default
10) bounds how many turns this process runs at once, but it does NOT bound
the provider's own account-level rate limit (requests/minute and/or
tokens/minute, depending on the provider). Watching
`agent_provider_requests_total{status="rate_limited"}` and
`agent_provider_retries_total` climb under normal traffic is the signal that
`max_concurrent_turns`, or the account's plan tier, needs to come down (or up)
to match what the provider's account actually sustains — sizing the
admission limiter is a customer-support / cost decision the platform
deliberately leaves to the operator, not something this fix can pick a
correct number for. A rate-limited account also means every retry adds
`services/outbox.py`'s own exponential backoff (`_RETRY_BASE_SECONDS *
2**attempt`, capped at `_RETRY_MAX_SECONDS`) on top of the provider's own
delay, so a persistently under-provisioned account is visible as slowly
growing, not instantly failing, end-to-end latency.

## Label hygiene

Labels are bounded, code-defined enums or operator-configured identifiers —
**never** a user id, phone number, correlation/request id, or message text.
Concretely: `runtime_id` (see below), `outcome`/`limit`/`direction` (fixed
string enums), `tool` (a connector name from the finite, operator-injected
`ToolRegistry` — **except** for a `denied` tool call, where the model's own
tool name was never checked against that registry at all; `_execute_tools`
(`agent/graph.py`) substitutes the fixed placeholder `_unrecognized_` for
that one case specifically, so an attacker- or hallucination-supplied tool
name can never reach the label store), and `provider`/`status`
(`observability/metrics.PROVIDER_NAMES`/`PROVIDER_REQUEST_STATUSES` — issue
#145's `agent_provider_requests_total`/`agent_provider_retries_total`: the
raw HTTP status code and any provider error body are never labels
themselves, only `classify_provider_status`'s fixed bucket). There is no
parameter on
`record_turn`/`record_tool_call`/`record_limit_trip`/`record_provider_request`/`record_provider_retry`
through which an unbounded value could reach a label —
`tests/test_observability_metrics.py`
and `tests/test_agent_runtime.py` both lock this down: one test pins the
exact label-name schema, one drives a real turn with a phone-number-shaped
`session_id` and asserts it never appears anywhere in the exported text
(`session_id` is never a label at all — it is not even a parameter of any
recording function), and another drives a `denied` tool call whose name is
an attacker-shaped payload (a phone number plus Prometheus exposition-format
metacharacters) and asserts neither the payload nor its PII substring ever
appears in the exported text.

## `runtime_id`

`AgentRuntime.__init__` accepts an optional `runtime_id: str | None`.
`None` (the default) falls back to this runtime's own derived provider
model id (`model_display_name(model)` — the same default slice 1's
`model_id` pricing override already uses), so every existing construction
site (`evals/runner.py`, direct library use via `agents_system.AgentRuntime`)
is unaffected. `main.py`'s lifespan — the only caller with a more
meaningful id on hand — passes the registered runtime id explicitly: the
opaque key the runtime was registered under, in `create_app(agents=...)`
or in `AGENT_REGISTRATIONS`. A real deployment's metrics are therefore
labeled by the id an operator actually chose, not by which underlying
model happens to be serving it.

## Testing pattern

Every metric is built against an explicit `prometheus_client.CollectorRegistry`
via `observability.metrics.build_metrics(registry)` — never bound to
`prometheus_client`'s own ambient default registry. `DEFAULT_METRICS`/
`DEFAULT_REGISTRY` are the process-wide set `GET /metrics` serves, built
once at import time. A test builds its own throw-away set —
`build_metrics(CollectorRegistry())` — so tests never collide with each
other (registering the same metric name twice on one registry raises
`ValueError`) or mutate process-wide state another test could observe. Pass
a fresh set into `AgentRuntime(..., metrics=my_metrics)` — or, for the
provider counters, into `main._build_chat_model(..., metrics=my_metrics)`
(issue #145) — to assert on exactly the values one test's turns produced.

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
- Provider throttling/retries (issue #145):
  `src/agents_system/observability/provider_http.py` (`MetricsHTTPTransport`,
  `classify_provider_status`), wired in `main._build_chat_model`'s
  `openai_compatible` branch; `tests/test_provider_http_metrics.py`,
  `tests/test_main.py` (the `#145` section), `tests/test_config.py`
  (`openai_compatible_request_timeout_s`)
- The actual #145 root cause (a missing demo schema, not the provider):
  `src/agents_system/services/reference.py` (`_fetch_rows`'s
  `SQLAlchemyError` handling), `tests/test_reference_backends.py` (the
  `#145` section), `tests/test_live_load_phase3.py`'s `seed_demo=True` fix;
  see `docs/platform/reference-backends.md`
