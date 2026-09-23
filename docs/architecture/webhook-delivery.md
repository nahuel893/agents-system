# Webhook delivery: durable persistence, recovery, and the deferred worker

W1 adds a PostgreSQL persistence primitive for inbound Meta messages and their future work records. W2b2 (see below) wires that primitive into the live route: `POST /webhook` now persists durably before acknowledging, and a background worker -- not the request -- runs the turn and sends the reply.

## W1 decision and data model

Migration `002` creates two new PostgreSQL tables:

| Table | W1 responsibility |
|---|---|
| `webhook_inbox` | Stores the unique Meta message ID, received payload, and receipt timestamp. |
| `outbox_work` | Stores one durable future-work row for an inbound message, including availability, lease, and completion fields. |

`outbox_work.inbound_message_id` is unique, so there is one work row for each accepted Meta message identity. The inbox's unique Meta message ID also makes repeated delivery of that identity converge on the already accepted row rather than creating another work row.

## Atomic acceptance

`accept_inbound_message` creates an inbox row and its work row in one database session, then commits them together. A successful acceptance therefore persists both records; a commit failure rolls the session back. A duplicate is recognized only for the inbox's Meta-ID uniqueness constraint after rollback; other commit failures propagate.

## Migration and downgrade boundary

Migration `002` is additive: it creates new, initially empty tables and indexes and does not alter existing audit data. It is therefore safe to apply before any W2 route wiring.

Its downgrade is intentionally destructive only after safety checks:

1. It refuses to run while any `outbox_work` row is incomplete (`completed_at IS NULL`), including pending or leased work.
2. Once no incomplete rows remain, it drops `outbox_work` and `webhook_inbox`.
3. That drop deletes completed work history as well.

Run the migration test only against a newly created, disposable PostgreSQL database. The test performs a downgrade and fails if `OUTBOX_TEST_DATABASE_URL` is unset. Never point it at an application or shared test database:

```bash
OUTBOX_TEST_DATABASE_URL="$ISOLATED_OUTBOX_DATABASE_URL" \
  PYTHONPATH=/home/nh/wt-44-webhook-worker/src \
  /home/nh/agents-system/.venv/bin/python -m pytest -q -m integration tests/test_outbox_migration_integration.py
```

The operator must first create the disposable database, set `ISOLATED_OUTBOX_DATABASE_URL` to its asyncpg URL, and remove the database after the test.

## W2a recoverable outbox state (storage only)

Migration `003` adds recoverable state without changing the live webhook route:

| Field | Responsibility |
|---|---|
| `lease_owner`, `lease_expires_at` | A worker identity and its committed, bounded claim. |
| `attempt_count`, `last_error`, `available_at` | Attempt history and the next retry time. |
| `failed_at` | A terminal, visible failure that is no longer claimable. |
| `outbound_body`, `outbound_send_key` | A generated reply and stable **internal** replay key, committed before a later provider send. |

`claim_available_outbox_work` reads PostgreSQL's `clock_timestamp()`, selects ready rows with `FOR UPDATE SKIP LOCKED`, sets a lease and increments the attempt count, then commits before returning any row. A concurrent worker therefore skips a live lease; a lease whose expiry has passed is eligible for recovery. An expired row already at `MAX_OUTBOX_ATTEMPTS` is terminalized with its audit event under that claim lock and is never returned for an extra attempt. Its committed claim outcome returns those terminalized rows separately from live claims, so a worker can invoke a deployment-owned notifier only after the durable audit/operator-action signal exists. W2a does not process a row, call Meta, or run a worker loop.

Before persisting outbound intent or recording a failure, W2a reloads and locks the row with a predicate requiring the caller's worker identity, non-terminal state, and an unexpired lease according to PostgreSQL's `clock_timestamp()`. A stale worker receives an explicit lease-lost error and cannot clear a recovered lease or overwrite reply state. A non-terminal failure clears its verified lease and commits a bounded exponential backoff with full jitter. It retains the outbound reply and internal send key: an ambiguous provider outcome must be retried, not deduplicated by that key. This is explicitly at-least-once delivery, not exactly-once delivery. The key is not a Meta Graph API idempotency field or header.

At `MAX_OUTBOX_ATTEMPTS`, W2a clears the lease, sets `failed_at`, preserves the error, and inserts an `audit_event` in the **same database session commit**. `audit_event` has a DEFAULT partition, so this direct insert remains partition-safe beyond the initially created monthly partitions. The durable event includes `operator_action: required`; its `alert_required` result is an actionable signal, not a claim that a human has been notified. W2b must connect that signal to the deployment-owned human escalation port or monitoring path.

Migration `003` is additive: it adds columns and recoverability indexes transactionally while retaining W1 indexes. W1 does not yet enqueue production work, so these indexes do not need a nontransactional concurrent build. Its downgrade acquires `ACCESS EXCLUSIVE` on `outbox_work` before checking W2a state and keeps that lock through index and column removal; it therefore refuses rather than racing to discard recoverability or persisted reply data. Like W1, execute its integration test only with `OUTBOX_TEST_DATABASE_URL` set to an isolated disposable PostgreSQL database.

## W2b1 deferred processor

W2b1 adds `DeferredWebhookWorker`, a directly testable processor for already-committed claims. W2b2 (below) wires its `start`/`stop` lifecycle into the live application; the processing logic described in this section is unchanged by that wiring. It loads the matching Meta ID from the stored signed envelope and fails closed if the directory is missing or a participant is unknown or inactive: it neither runs a turn nor sends a reply. A missing directory or unexpected normalization failure is retryable and can become a visible terminal failure; an invalid address (`ValueError`), unknown/inactive participant, successful provider send or empty agent reply uses a fenced completion transition under the worker's live database-time lease. A non-text message (image, audio, location, document, ...) is durably accepted and completed the same way, with no turn and no reply, and its Meta `type` is logged explicitly (`webhook_worker.non_text_message`); replying to a non-text message is a product follow-up, not something this worker attempts.

Database lookup, runtime, turn, intent persistence, completion, and provider-send failures use W2a's fenced `record_outbox_failure` transition. A lease lost at any fenced transition does not mutate the stale claim. The default lease is bounded at ten minutes, exceeding the configured sub-300-second turn timeout plus provider-send allowance; this reduces premature recovery but does not create exactly-once execution.

Before a provider call, a generated reply is committed as `outbound_body` with a stable internal key derived from the Meta message ID. A later claim with that persisted reply sends the same body without rerunning the agent. The key is intentionally not placed in the Meta Graph API request: outbound delivery is at least once and an ambiguous provider result can produce a duplicate message.

Conversation recording remains best effort in an independent session. A deployment can inject an async terminal-failure notifier. W2b1 invokes it only after W2a has committed the terminal row and same-transaction audit/operator-action signal, including terminalization discovered during claim recovery; a missing or failing callback does not claim human notification and leaves that durable action signal intact. The current schema has no separately durable notification-attempt state, so it cannot guarantee retry of the human callback.

## W2b2 the live route: persist before ack, never process in-request

`POST /webhook` (`integration/webhook.py`) now does exactly three things, in this order: verify the HMAC signature over the raw body, walk the parsed payload for every valid Meta message id, and durably persist each one through `accept_inbound_message` before returning `{"status": "ok"}`. It never runs a turn, never calls the participant directory or conversation recorder, and never touches the WhatsApp client -- all of that moved to `DeferredWebhookWorker`, entirely outside the request.

A "valid Meta message" is any entry reachable at `entry[].changes[].value.messages[]` carrying a non-empty `id`. A status-update event, an empty/absent `messages` array, or a message missing its id is not persisted, matching the pre-W2b2 route's own always-200 handling of those shapes. The **full** parsed payload is stored against every message id found in it (not just that one message's fields), because a batch can carry several messages under one Meta signature and the worker later has to find the exact message a given id refers to inside that shared envelope (`webhook_worker._extract_inbound_turn`, from W2b1).

**HMAC first, unconditionally.** `verify_signature` runs before `json.loads` and before any persistence call. A forged or missing signature is rejected with 403 and never reaches the database, regardless of payload shape.

**Persist every valid message; 503 on any failure.** Messages in a batch are persisted in delivery order, one `accept_inbound_message` call each. The first one whose commit raises aborts the request with `HTTPException(503)` -- Meta retries the whole envelope on a 5xx, and this is the one case in this route where that is exactly the desired outcome (a transient DB failure), not the poison-message loop the old route's always-200 contract (AD-2) existed to avoid for attacker-controlled *content*. Messages already committed **before** that failure stay committed: they are not rolled back or re-attempted in the same request. On Meta's retry of the same envelope, `accept_inbound_message` recognizes each already-committed id (`webhook_inbox.meta_message_id` is unique) and returns `duplicate=True` instead of raising, so the retry naturally resumes at the message that actually failed rather than re-doing work or erroring on the ones that already succeeded.

**A malformed or attacker-controlled body never persists anything and never 500s.** `json.loads` failures and every payload-shape mismatch the old route already defended (`entry` not a list, `changes` not a list, `value` not a dict, and so on) short-circuit to `{"status": "ok"}` before any DB call, exactly as before.

## W2b2 lifespan wiring: start after dependencies, stop before teardown

`main.py`'s `lifespan` constructs one `DeferredWebhookWorker` per process and starts its poll loop only after every dependency it needs already exists on `app.state`: the engine, the WhatsApp client, the resolved runtime cache, and the participant directory / conversation recorder `create_app` stashed there. It is skipped -- with a `webhook_worker.no_runtime_resolved` warning, not a startup failure -- when `whatsapp_runtime_id` is unset or resolves to nothing in the runtime cache; durable inbound work still accumulates and simply waits for an operator to fix the configuration, it is never dropped.

`webhook_worker.stop` is pushed onto the lifespan's `AsyncExitStack` last, so its LIFO teardown runs the worker's stop **first**, before the engine is disposed, the WhatsApp client is closed, or Redis's pool is closed -- the worker never has a dependency pulled out from under an in-flight iteration.

`DeferredWebhookWorker.start()`/`.stop()` mirror `AuditSink`'s existing background-task shape: `start()` spawns an `asyncio.Task` running `process_available` on a fixed interval (`webhook_worker_poll_interval_s`, default 1s; batch size `webhook_worker_claim_limit`, default 10 -- both in `Settings`), and `stop()` **hard-cancels** that task rather than draining it. This is deliberate, not a shortcut: a claim cancelled mid-flight (mid-turn, mid-send) leaves its lease exactly as W2a committed it, so a fresh claim -- this worker restarting, or any other -- recovers it once that lease expires. `process_claimed_work` already lets `asyncio.CancelledError` propagate instead of recording a failure (it is a `BaseException`, not caught by the broad `except Exception` around each processing step), so a hard-cancelled loop never writes a spurious failure row; recovery is W2a's lease-expiry primitive, reused as-is, not reimplemented here.

## Dedup decision: the Redis check is superseded, not duplicated

Before W2b2, `POST /webhook` called `services.dedup.is_duplicate` (Redis `SET NX EX`, 5-minute TTL, **fails open** on a Redis error) to skip an already-processed `message_id`. W2b2 removes that call from the route. The durable inbox's `webhook_inbox.meta_message_id` unique constraint -- enforced transactionally by `accept_inbound_message` -- is now the sole duplicate-delivery guard on the live path, and it strictly dominates the Redis check: it is not bounded by a TTL a slow retry can outlive, and a persistence failure returns 503 (Meta retries) rather than silently reprocessing the way a downed Redis used to. `services/dedup.py` and its `DEDUP_TTL_SECONDS` constant remain in the tree (see that module's docstring) only because `main.py` still asserts an unrelated static invariant against `DEDUP_TTL_SECONDS`, tying the platform's default turn-execution timeout to it; that invariant guarded the old synchronous route (a turn running in-request for longer than the dedup TTL), a scenario that cannot recur once no turn ever runs in-request. Untangling that invariant is out of scope for this slice and left as a follow-up.

## W3 bounded admission control (#46, ADR-001 D-033)

Nothing bounded how many turns could run at once. `DeferredWebhookWorker.process_available` used to claim up to `webhook_worker_claim_limit` rows and process them one at a time, sequentially -- an accident of the loop shape, not a designed bound, and it said nothing about `POST /v1/chat/completions` (`integration/openai_adapter.py`), which calls `AgentRuntime.run_turn` directly, once per HTTP request, with no limit at all. A burst of arriving conversations across either path could open one turn per conversation and exhaust the database pool regardless of process count.

`main.py`'s lifespan now builds exactly one `TurnAdmissionLimiter` (`services/admission.py`), sized from `Settings.max_concurrent_turns`, and shares it -- via `app.state.turn_admission_limiter` -- between both entry points: the webhook worker gates each claimed item's processing on it, and the adapter wraps its `run_turn` call in it too. The bound is therefore process-wide, not per-entry-point.

**The approved decision is backpressure, never rejection.** At capacity the webhook worker claims nothing: `process_available` caps the claim at the limiter's free slots (`min(limit, available)`) and returns early when that is zero, so an already-durably-persisted message simply waits, unclaimed, in the outbox -- no lease is taken and no attempt is spent on work that cannot start yet. It is claimed on a later poll once a slot frees. This is why the limiter is a purpose-built primitive rather than a bare `asyncio.Semaphore`: the worker needs to know how many slots are free *before* it claims, which a semaphore does not expose. Once claimed, items are processed concurrently (`asyncio.gather`), each gated by the same limiter, so a worker that used to run one turn at a time can now genuinely run several.

The default, `10`, matches ADR-001's Stage A launch target ("10 concurrent conversations: a single process") and stays under a single engine's default pool ceiling (`pool_size=5, max_overflow=10`). Correction from independent review: a turn does **not** hold a connection only "per discrete operation" -- `agent/graph.py::_execute_tools` opens one `AsyncSession` and holds it across **every** tool call in a round (sequential, never concurrent: a shared `AsyncSession` must not be used from more than one coroutine at once), so a round with several slow sequential tool calls can hold that one connection for the round's full cumulative latency, not a single query's. This worker's own bookkeeping (`_load_inbound`, `_resolve_participant`, `persist_outbound_intent`, `_complete_or_retry`, `_record_failure`) genuinely is session-per-call. Either way, a turn holds **at most one** connection at any given instant -- never one per tool call, since the loop is sequential -- so `max_concurrent_turns` concurrent turns implies at most that many held connections from this path at once, still under the pool's ceiling; it says nothing about how long any one of them is held. Sizing the pool itself to match production concurrency is #45's job, not this one's.

## Relation to ADR-002 G.4/G.22

This document uses the requested G.4/G.22 shorthand. In the current ADR, G.22 covers durable chat history and points to A.4, “Durable working memory.” W1 chooses PostgreSQL as the inbound-handoff source of truth for the inbox and outbox. It does **not** decide the durable chat-history backend.

## Rollback boundary

Migration `003` refuses to remove non-default W2a state. Migration `002` still refuses to remove incomplete work. Once all work is completed and W2a state is absent, the downgrades remove the new tables and completed history; the existing `audit_event` table remains untouched.
