# W4 — Runtime, deployment, and generic agents

Status: proposed, 2026-08-17. Supersedes nothing; W3 is closed.

This is the work path for the four themes raised after W3 closed: independent
client sessions, concurrency guards, deployability, and inheritable generic
agents. It is ordered by dependency, not by priority — the sequence exists
because some of these decisions constrain the others, and getting them out of
order means writing code twice.

## Why this order

The instinct is to build the deployment first, then add guards. It is the wrong
way round, for one concrete reason: **the deployment topology decides where the
guards can live.**

An in-process semaphore limits one process. Run four uvicorn workers and there
are four independent semaphores, so the real ceiling is 4× whatever number was
written down — and nothing reports the discrepancy. A limiter that holds across
N processes has to keep its counter in Redis. Same for rate limiting, same for
any admission control.

So the topology decision comes first, the guards are built against it, and the
deployment artifacts come last because by then there is something correct to
deploy.

The webhook fix sits between them, because it is what creates the place a guard
can act on. Today there is no queue to admit into — the request *is* the work.

## Current state, measured

Verified against the code on `main` at `67592cb`, not from memory.

| Property | State | Where |
|---|---|---|
| Per-client session isolation | **Works.** `thread_id=phone`, one Redis thread per client | `integration/webhook.py` |
| Session TTL | **Works.** 24h sliding (`refresh_on_read: True`) | `config.py:91`, `main.py:31` |
| Session invalidation on order confirm | **Missing.** TTL is the only expiry | — |
| Checkpointer degradation | **Works.** Redis failure retries the turn stateless | `agent/graph.py:281` |
| DB pool sizing | **Default.** 5 + 10 overflow = 15 connections, `pool_timeout=30s` | `models/base.py:21`, `services/medallion.py:19` |
| Concurrency limit / rate limiting | **None anywhere.** `rg -i 'semaphore\|rate_limit\|limiter\|throttl\|max_concurren' src/` → 0 hits | — |
| Uvicorn workers | **Unconfigured.** One process, one event loop | — |
| Webhook response ordering | **Defect.** Agent turn (budget 60s) and outbound send both complete *before* the 200 | `integration/webhook.py` |

### The defect, stated precisely

`webhook.py` marks `message_id` in the Redis dedup set, then awaits
`runtime.run_turn(...)` — whose budget is `total_execution_timeout_s = 60`
(`harness/loader.py:114`) — then awaits the outbound send, and only then returns
200.

`services/dedup.py:10` says out loud that Meta retries:
`DEDUP_TTL_SECONDS = 300  # 5 minutes — matches Meta's webhook retry window`.
When the 200 arrives too late, Meta retries; the retry hits the dedup mark and
is discarded. The customer's message is dropped.

The code comments anticipate this for a *crash*
("Meta's retry gets absorbed by dedup"). A slow turn produces the same outcome
and is not covered. And under load every turn is slow: with a 15-connection pool
and no admission control, `pool_timeout` alone is 30s before any LLM call.

**The three capacity findings and the ordering defect converge on the same
symptom — silently dropped customer messages.** That is what makes this the
first work item after D-007, ahead of anything about capacity.

## Task path

Each row is a delegation-ledger candidate. Line estimates are for planning, not
promises; anything over ~400 needs a chain decision per the `chained-pr` rule.

### Phase 0 — finish D-007 (in flight)

| ID | Slice | Depends on | Est. | Note |
|---|---|---|---|---|
| D-007-pr1 | Alembic infra, `AuditEvent`, table-ownership rule, partition lifecycle | — | 1282 | **Done, verified.** Size decision pending. |
| D-026 | Audit events + redactor (Pydantic discriminated union, PII default-deny) | D-007-pr1 | 1711 | **Done, verified.** 606 green on `feat/D-007-pr2-events-redactor`. |
| D-027 | Audit sink wiring through injector, interceptor, factory, graph | D-026 | 1122 | **Done, verified.** 633 green on `feat/D-007-pr3-sink-wiring`. Four chained silent defects fixed; documented in `docs/platform/audit.md`. |
| D-028 | Monthly partition job — operational entrypoint with its own connection | D-007-pr1 | ~80 | **No longer critical**: the DEFAULT partition means missing a run costs query pruning, not availability. Was a hard dependency before. |
| D-041 | `AuditSink.stop()` must await the drainer instead of cancelling it | D-027 | ~40 | Graceful shutdown currently discards the queue tail. Pinned as a strict xfail, so the fix is forced to remove the marker. Belongs here because the shutdown handshake is a deployment concern. |

### Phase 1 — decide the topology

| ID | Slice | Depends on | Est. |
|---|---|---|---|
| D-029 | Runtime topology decision record: process count, where shared state lives, what a restart loses, what scales horizontally | — | ~150 (docs) |

Not code. The output is an ADR that answers: one process or N workers; if N,
which state must move out of process (admission counters, rate limits, the
embedder if it stays local); and what the target concurrent-conversation number
actually is. Every task below reads its answer.

Open question this must settle: `LocalBGEEmbeddingProvider.embed` runs
`SentenceTransformer.encode(batch_size=32)` on the default
`ThreadPoolExecutor` — one shared model, `min(32, cpu_count+4)` threads, CPU and
RAM bound. With the OpenAI provider the same path is pure I/O and scales. That
is a topology choice with a hosting cost attached, not an implementation detail.

### Phase 2 — fix the ordering defect

| ID | Slice | Depends on | Est. |
|---|---|---|---|
| D-030 | Webhook returns 200 before the turn runs; turn and send move to background | D-029 | ~350 |
| D-031 | Dedup claim/release so a failed or timed-out turn does not permanently swallow the message | D-030 | ~200 |

D-030 and D-031 are separable but must land together to be correct: moving the
turn to background without fixing the dedup claim just moves where the message
is lost. D-031 needs a real decision — a claim that a crashed worker releases,
versus a mark that only expires — and that is a durability question, so it reads
D-029's answer about what a restart loses.

### Phase 3 — guards

| ID | Slice | Depends on | Est. |
|---|---|---|---|
| D-032 | Pool sizing: `pool_size`/`max_overflow`/`pool_timeout` from settings, documented defaults, both engines | D-029 | ~120 |
| D-033 | Admission control: bounded concurrent turns, in-process or Redis-backed per D-029 | D-030 | ~300 |
| D-034 | Per-client rate limiting, so one number cannot consume the whole budget | D-033 | ~250 |
| D-035 | Session invalidation on order confirmation | — | ~150 |

D-035 is independent and can run any time. It closes the gap in the session
model: the 24h TTL is the floor for an abandoned cart, but a confirmed order
must clear the thread so the next conversation does not start contaminated with
the previous one's state. Two mechanisms, one currently built.

On D-032, keep the sliding TTL at 24h rather than the 1h that prompted this.
`refresh_on_read` makes the window one of *inactivity*, and an hour of
inactivity is ordinary for a shop owner serving a counter — they would lose a
half-built order.

### Phase 4 — deployable

| ID | Slice | Depends on | Est. |
|---|---|---|---|
| D-036 | Server provisioning: compose/systemd units, migrations on deploy, health checks, log destination | D-029 | ~400 |
| D-037 | Provider and model configuration per agent, no code edits, no secrets in versioned files | — | ~300 |
| D-038 | Configuration TUI over D-037 | D-037 | ~500 |

D-037 has a head start: `platform/roles/*/manifest.md` and
`deployments/<client>/<role>/` already exist as the configuration surface. The
work is making model and provider selection read from there, and validating it
at startup instead of at first request.

D-038 is last on purpose. It is ergonomics over a mechanism that already works —
a TUI that writes manifests. Valuable, but it must not consume the budget of the
things that keep messages from being dropped.

Note for D-036: `alembic.ini` no longer carries a URL (see D-007-pr1), so
`alembic upgrade head` is a deployable step driven by `DATABASE_URL`.

### Phase 5 — generic inheritable agents

| ID | Slice | Depends on | Est. |
|---|---|---|---|
| D-039 | Audit what the four platform roles actually do end to end | — | ~200 |
| D-040 | Multi-level and multi-role composition | D-039 | ~450 |

**Inheritance already exists**, which reframes this theme. `harness/injector.py:29`:

```python
effective = set(definition.permissions) & set(granted_permissions)
```

An intersection. The platform role declares the maximum surface; a deployment
can only narrow it. That answers the design question directly — tools and
permissions are inherited, and a child cannot reach into the detail of any of
them, only drop it. The invariant is *injection can only subtract*, and W3
already hardened it.

Two real gaps remain, and they are what D-040 is:

1. Inheritance is **one level**. There is no chain of three, and no composition
   of several roles into one.
2. A typo'd deployment name **widens** the surface instead of failing —
   `loader.py:670` falls through to the generic role when `load_override`
   returns `None`, granting the role's full allowance rather than the
   deployment's subset. Filed as a known defect in `delegations.md` and still
   open. Multi-level composition multiplies the blast radius of that fallback,
   so it should be closed first or in the same slice.

D-039 comes first because `sales-agent` has been exercised end to end and the
other three have not, to a degree this session did not establish. Building
composition on roles whose behavior is unverified means debugging two things at
once.

## Not in this path

- Anything requiring `MEDALLION_DB_*` or the host-hardening run — both are
  blocked on the human, tracked in `delegations.md`.
- Foreign-language token leakage in model output (known defect, unfiled work).
