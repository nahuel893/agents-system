# ADR-005 — Operational safety and governance roadmap

**Status:** Proposed · **Date:** 2026-09-25

## Summary

This is a roadmap, not a design: a survey of nine operational-safety areas
against the code as it stands today, each with what exists (with file:line
references, verified against the current tree), the gap, a short proposed
direction, and a priority. It proposes no implementation and creates no
issues. Where a capability does not exist, this says so plainly instead of
assuming it.

## Context

ADR-003 closed the permission-tier model. ADR-004 (reserved) will define the
library-first-agents contract — predefined roles a downstream project
installs and cannot silently reshape. Between those two, several operational
concerns were deferred rather than decided: what actually stops an agent
before it acts, what bounds a runaway turn, what is logged and for how long,
and what a human ever sees before a MAJOR version ships. This ADR maps that
surface once, so follow-up changes have a shared reference instead of each
rediscovering the same code.

## Roadmap

### 1. Security

**What exists:** an explicit `T0`–`T3` tier hierarchy (`harness/registry.py:9-33`);
the `untrusted_input` barrier rejecting T3 grants for untrusted roles
(`harness/loader.py:1580` at load, `harness/factory.py:349-361` at grant time, ADR-003 R4). Every T3 call runs inside a
`bwrap` sandbox with `prlimit` limits and no unsandboxed fallback
(`docs/operations/sandbox-bwrap.md`, ADR-002 C.14). Secrets load through
`pydantic-settings` (`config.py:55-91`) and boot fails closed on an empty
`meta_webhook_secret` or an exposed adapter with no `adapter_api_key`
(`config.py:219-245`). Webhook auth is HMAC (`integration/meta_signature.py:14`,
called at `integration/webhook.py:130`); adapter auth is a constant-time
Bearer check (`integration/openai_adapter.py:82-108`). Dependency CVEs are
tracked as issues #32–#35, gated by the `dependency-audit` CI job's
`--ignore-vuln` lines and weekly Dependabot PRs (`.github/dependabot.yml`).

**Gap:** no consolidated threat-model document exists. `untrusted_input` has
one structural invariant test (`tests/test_untrusted_input_invariant.py`) but
no adversarial prompt-injection eval corpus. #32–#35 remain open pending
coordinated major-version migrations.

**Direction:** add a short threat-model doc naming trust boundaries, plus a
minimal adversarial prompt-injection eval set.

**Priority:** P1 · **Dependency:** none for the doc; #32 blocks full CVE
closure on the LangChain/LangGraph 1.x migration.

### 2. Guards

**What exists:** Layer-1 excludes ungranted tools at build time
(`harness/injector.py:122`). Layer-2's `intercept()` revalidates sensitive
tools against the persisted `deploy_grant_ceiling`, not the role's full
declared set (`harness/interceptor.py:77-149`; ceiling built at
`harness/factory.py:380`, read at `agent/graph.py:478`). `escalation_rules`
is rendered into the system prompt as model-facing guidance only
(`harness/factory.py:199-227`) — not a code-enforced gate. `EscalationChannel`
(`services/escalation.py:24`) and `OrderWriter` (`services/orders.py:24`) are
both empty `Protocol` stubs with no reference implementation.

**Gap:** no human-in-the-loop interrupt exists before a T2/T3 action; nothing
today actually stops an order write pending confirmation. `intercept()`
validates `tool_name` and permissions only — tool *arguments* reach the
connector unchecked beyond the model's own tool-call typing.

**Direction:** add a real confirmation step gating T2 writes behind
`EscalationChannel`, and a declarative argument-policy check evaluated in
`intercept()` before dispatch.

**Priority:** P1 · **Dependency:** a minimal `OrderWriter` reference
implementation to have something concrete to gate.

### 3. Execution limits

**What exists:** `_validate_execution_limits` (`harness/loader.py:1611-1645`)
enforces that an override may only tighten `_PLATFORM_DEFAULT_LIMITS`
(`harness/loader.py:156-162`: `tool_call_timeout_s=10`,
`total_execution_timeout_s=60`, `max_tool_calls=20`,
`max_delegation_depth=2`, `max_clarification_attempts=3`).
`_effective_limits` (`agent/graph.py:61-74`) merges per-key over those
defaults; the graph enforces `max_tool_calls` and both timeouts, but not
`max_delegation_depth`/`max_clarification_attempts` (noted directly at
`agent/graph.py:53-54`). `main.py`'s `lifespan` builds one process-wide
`TurnAdmissionLimiter` (`services/admission.py:48-56`, wired at
`main.py:112`), bounding concurrent turns only.

**Gap:** no per-turn or per-conversation token/cost budget, no per-principal
rate limit, no loop/repetition detection beyond the flat `max_tool_calls`
ceiling, no circuit breaker on repeated tool or provider failures.

**Direction:** add token/cost accounting to turn state, a per-principal
limiter alongside `TurnAdmissionLimiter`, and a consecutive-failure circuit
breaker per tool.

**Priority:** P1 · **Dependency:** none; extends the existing
`_effective_limits`/`TurnAdmissionLimiter` seams directly.

### 4. Data governance

**What exists:** `Redactor` always strips phone numbers and emails from
audit payloads, and redacts `message`/`body`/`text` unless
`audit_policy.capture_tool_input` is set (`audit/redactor.py:24-45`).
`audit_event` is a monthly-range-partitioned table with a DEFAULT partition
so writes never fail closed on a missing partition
(`alembic/versions/001_add_audit_event.py`); durable outbox state lives in
`models/outbox.py`/`services/outbox.py`. `role_is_read_only`
(`services/db_role.py:19`) asks Postgres directly whether the BI role can
write, checked in CI by the `bi-readonly` job.

**Gap:** no retention or deletion policy for `audit_event`/outbox rows —
partitioning exists for query pruning, not automated retention. No data
classification beyond tool-danger `Tier`. `deployments/` is empty in this
public repo (README only), so per-deployment isolation is a convention, not
enforced code.

**Direction:** add a scheduled partition-drop/retention job with a
documented period, and a short data-classification note separating PII from
operational data.

**Priority:** P2 · **Dependency:** none.

### 5. Logging

**What exists:** `structlog` configured for JSON output with ISO timestamps
(`observability/logging.py:16-27`); events follow a `component.event_name`
convention (`loader.invariant_violation`, `interceptor.call_blocked`,
`health.postgres_error`). `request_id` is generated per inbound HTTP request
and bound to structlog contextvars by middleware
(`observability/middleware.py:22-23`); `audit/recorder.py` reads it back as
the audit correlation id, defaulting to `"none"` when absent
(`audit/recorder.py:60-62`). `Redactor` is the same redaction path for audit
events.

**Gap:** `services/webhook_worker.py` never binds `request_id` (or any
conversation id) to structlog context, so a turn the outbox worker executes
later logs and audits with correlation id `"none"` — the webhook → outbox →
turn chain is not actually correlated today, only the initial HTTP request
is.

**Direction:** bind a stable conversation/outbox-row id to structlog
contextvars at the start of the worker's claim loop and thread it into every
`record_*` call for that turn.

**Priority:** P1 · **Dependency:** none.

### 6. Observability

**What exists:** `langsmith` is present only as a transitive dependency of
`langchain-core` via `langgraph` (`uv.lock:956`) — not a direct
`pyproject.toml` dependency. No `opentelemetry` or
`langsmith`/`LANGCHAIN_TRACING` import exists anywhere under `src/`. No
metrics library or `/metrics` endpoint exists in `main.py`.
`evals/reporting.py::write_results` writes eval results to a file; nothing
in `src/` reads that output back into a runtime signal.

**Gap:** zero traces, zero metrics; eval results are a standalone artifact
disconnected from runtime monitoring.

**Direction:** wire LangSmith tracing behind an explicit settings flag first
(the dependency is already resolved), then add a minimal counter set (tool
calls, denials, limit trips) before any broader OpenTelemetry rollout.

**Priority:** P3 · **Dependency:** none blocking; the counters areas 3 and 2
produce are what area 7's alerts would consume.

### 7. Monitoring and alerts

**What exists:** `GET /health` (`main.py:641-720`) probes Postgres and Redis
with a 3s timeout, reports `webhook_worker.running` and outbox
`pending`/`leased`/`leased_expired` counts, and degrades — not merely
reports — when work is pending or lease-expired with no worker running
(`main.py:686-706`, issue #141).

**Gap:** no SLO document exists. Nothing polls `/health` and alerts on it —
it is pull-only; no push-based alerting path (email, Slack, PagerDuty, or
similar) exists anywhere in the repo. No alert conditions are defined for
denied-tool spikes, limit trips, or provider errors/cost anomalies, since
areas 3 and 6 do not yet emit those signals. Once area 9 exists, four more
conditions have no hook either: sustained admission saturation (`in_flight`
at `max_concurrent_turns`, `services/admission.py:62-77`), outbox backlog
age rather than just its count (`count_outbox_backlog` only counts,
`services/outbox.py:217-264`), per-participant quota-trip counts, and
tokens/cost per turn.

**Direction:** define 2–3 initial SLOs (webhook processing latency, time
spent degraded) and wire `/health` into an alerting tool as the first path;
add denied-tool-spike and limit-trip alerts once areas 3 and 6 land, and
admission-saturation, backlog-age, participant-trip, and cost-per-turn
alerts once area 9 lands.

**Priority:** P2 · **Dependency:** areas 3, 6, and 9 for the richer alert
conditions; the `/health`-based SLO can start independently.

### 8. Governance of agents

**What exists:** SemVer/CHANGELOG is automated via release-please
(`.github/workflows/release-please.yml`, `release-please-config.json`);
issue #18 is closed and the version now moves from Conventional Commit
prefixes (`docs/operations/release-process.md`). `ToolGranted`/`ToolDenied`
audit events are recorded at injection time (`audit/events.py:50-63`,
`audit/recorder.py:179-236`) — a grant/denial trail exists at the tool
level. No `CONTRIBUTING.md` or PR template exists; review runs through
ordinary GitHub PR review plus required `ci`/`secret-scan`/`dependency-audit`
checks.

**Gap:** nothing inspects a diff and forces a MAJOR (`feat!:`/`fix!:`) bump
when a predefined role's `tools:`/`permissions:` widen — a manifest change
could ship under an ordinary `feat:`/`fix:` commit today. This is exactly
library-first-agents' (ADR-004) concern.

**Direction:** add a CI check diffing `platform/roles/**/manifest.md`
tool/permission lists against `main`, requiring a breaking-change marker
when they widen; land it alongside ADR-004.

**Priority:** P2 · **Dependency:** ADR-004 defines the role/version
contract this check would enforce.

### 9. Concurrency and scale

**What exists:** inbound WhatsApp messages are durably persisted to the
Postgres outbox and the webhook answers immediately
(`services/outbox.py:88-143`); the worker claims only as many rows as free
`TurnAdmissionLimiter` slots allow (`services/webhook_worker.py:194-198`) —
backpressure, never rejection — and `pending_outbox_statement`
(`services/outbox.py:164-214`) enforces strict per-`conversation_key`
ordering, so one sender's messages always run one at a time.
`TurnAdmissionLimiter` (`services/admission.py:45,48-111`,
`DEFAULT_MAX_CONCURRENT_TURNS = 10`) is built once per process
(`main.py:112-114`) and shared, via `app.state`, by the webhook worker and
the OpenAI adapter. Per-conversation history lives in an `AsyncRedisSaver`
LangGraph checkpointer keyed by the normalized phone number
(`services/webhook_worker.py:299`), governed only by a time-based TTL
(`checkpointer_ttl_s = 86400`, `refresh_on_read: True`, `config.py:106`,
`main.py:41-47,69-71`) — not a size- or token-based bound. `AgentRuntime`
binds tool schemas once at construction (`agent/graph.py:379-382`), but
each `bound_model.ainvoke()` call still carries the system prompt, those
schemas, and the full accumulated `state["messages"]` fresh over the wire
(`agent/graph.py:97`).

**Gap:** four compounding risks surface once this is examined at
100-concurrent-sender scale. (1) Admission is enforced per *process* via an
in-memory counter (`services/admission.py:56`): N replicas each allow up to
10 concurrent turns and up to 15 DB connections
(`pool_size=5 + max_overflow=10`, `models/base.py:86-88`, SQLAlchemy
defaults), with nothing capping the sum across replicas. (2) No per-sender
or per-deployment quota exists — a single insistent or abusive sender
consumes admission slots exactly like everyone else, since
`TurnAdmissionLimiter` and `pending_outbox_statement` are both
principal-blind. (3) `AgentState.messages` uses LangGraph's `add_messages`
reducer with no cap (`agent/state.py:11`); combined with the TTL's
`refresh_on_read`, an active conversation's checkpointed history is never
trimmed, windowed, or summarized — it grows unbounded for as long as the
conversation stays active, raising cost, latency, and eventual
context-window failure risk. (4) That unbounded, uncached context is resent
on every model call, so per-turn cost and latency rise with both
conversation age and concurrent volume; #59 already trimmed one source of
this for the `run_report` tool (`connectors/report_connector.py:116-148`,
closes #56), but no broader caching or measurement exists.

**Direction:** (1) a shared, distributed admission limiter (Postgres- or
Redis-backed) or a claim-time global cap across replicas. (2)
per-participant and per-deployment quotas enforced at claim time, with an
operator-visible signal when a quota trips. (3) a windowing or
summarization policy per role, applied before `_call_model` builds
`model_input`. (4) provider-side prompt caching for the static prefix,
further tool-schema trimming, and measuring tokens-per-turn as an actual
metric.

**Priority:** P1 · **Dependency:** none; each extends an existing seam
(`TurnAdmissionLimiter`, `pending_outbox_statement`, `_effective_limits`)
directly.

## Sequencing

1. Correlation IDs end-to-end — bind a conversation/outbox id into the
   webhook worker's structlog context (area 5).
2. Threat-model doc + prompt-injection eval corpus — no code change,
   informs review of everything after it (area 1).
3. Human-in-the-loop confirmation for T2/T3 actions, plus tool-argument
   policy checks in `intercept()` (area 2).
4. Per-turn/conversation token and cost budgets, per-principal rate
   limiting, and a circuit breaker (area 3).
5. Distributed admission limiting, per-participant/deployment quotas,
   conversation-history windowing, and model-call cost reduction (area 9).
6. Continue closing #32–#35 as their coordinated migrations land (area 1,
   already tracked).
7. Audit/outbox retention job and a short data-classification note (area 4).
8. LangSmith trace flag and a minimal `/metrics` counter set (area 6).
9. SLOs and `/health`-based alerting, extended with the counters from 4, 7,
   and 9 (area 7).
10. CI check enforcing a MAJOR bump on role tool/permission changes,
    alongside ADR-004 (area 8).
