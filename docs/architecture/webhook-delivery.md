# Webhook delivery: W1 durable persistence boundary

W1 adds a PostgreSQL persistence primitive for inbound Meta messages and their future work records. It does not change the live webhook path: it remains synchronous and Redis-backed until W2 wires a durable route and worker.

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

`claim_available_outbox_work` reads PostgreSQL's `clock_timestamp()`, selects ready rows with `FOR UPDATE SKIP LOCKED`, sets a lease and increments the attempt count, then commits before returning any row. A concurrent worker therefore skips a live lease; a lease whose expiry has passed is eligible for recovery. An expired row already at `MAX_OUTBOX_ATTEMPTS` is terminalized with its audit event under that claim lock and is never returned for an extra attempt. W2a does not process a row, call Meta, or run a worker loop.

Before persisting outbound intent or recording a failure, W2a reloads and locks the row with a predicate requiring the caller's worker identity, non-terminal state, and an unexpired lease according to PostgreSQL's `clock_timestamp()`. A stale worker receives an explicit lease-lost error and cannot clear a recovered lease or overwrite reply state. A non-terminal failure clears its verified lease and commits a bounded exponential backoff with full jitter. It retains the outbound reply and internal send key: an ambiguous provider outcome must be retried, not deduplicated by that key. This is explicitly at-least-once delivery, not exactly-once delivery. The key is not a Meta Graph API idempotency field or header.

At `MAX_OUTBOX_ATTEMPTS`, W2a clears the lease, sets `failed_at`, preserves the error, and inserts an `audit_event` in the **same database session commit**. `audit_event` has a DEFAULT partition, so this direct insert remains partition-safe beyond the initially created monthly partitions. The durable event includes `operator_action: required`; its `alert_required` result is an actionable signal, not a claim that a human has been notified. W2b must connect that signal to the deployment-owned human escalation port or monitoring path.

Migration `003` is additive: it adds columns and recoverability indexes transactionally while retaining W1 indexes. W1 does not yet enqueue production work, so these indexes do not need a nontransactional concurrent build. Its downgrade acquires `ACCESS EXCLUSIVE` on `outbox_work` before checking W2a state and keeps that lock through index and column removal; it therefore refuses rather than racing to discard recoverability or persisted reply data. Like W1, execute its integration test only with `OUTBOX_TEST_DATABASE_URL` set to an isolated disposable PostgreSQL database.

## Explicitly deferred to W2b

The live webhook is still synchronous and Redis-backed. W2a does **not** implement a live worker, early HTTP acknowledgement, HTTP 503 behavior, provider sends, or admission control. It persists claim and retry state only; W2b must process the claim, persist a generated outbound reply before a send, retry ambiguous provider outcomes at least once, and connect terminal `alert_required` signals to deployment-owned escalation.

## Relation to ADR-002 G.4/G.22

This document uses the requested G.4/G.22 shorthand. In the current ADR, G.22 covers durable chat history and points to A.4, “Durable working memory.” W1 chooses PostgreSQL as the inbound-handoff source of truth for the inbox and outbox. It does **not** decide the durable chat-history backend.

## Rollback boundary

Migration `003` refuses to remove non-default W2a state. Migration `002` still refuses to remove incomplete work. Once all work is completed and W2a state is absent, the downgrades remove the new tables and completed history; the existing `audit_event` table remains untouched.
