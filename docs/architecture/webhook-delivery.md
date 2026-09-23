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
  PYTHONPATH=/home/nh/wt-43-webhook-outbox/src \
  /home/nh/agents-system/.venv/bin/python -m pytest -q -m integration tests/test_outbox_migration_integration.py
```

The operator must first create the disposable database, set `ISOLATED_OUTBOX_DATABASE_URL` to its asyncpg URL, and remove the database after the test.

## Explicitly not implemented in W1

The live webhook is still synchronous and Redis-backed. W1 does **not** implement a live worker, early HTTP acknowledgement, HTTP 503 behavior, retries, or admission control. The stored work-selection query is preparation for W2; it does not claim, process, or send work.

## Relation to ADR-002 G.4/G.22

This document uses the requested G.4/G.22 shorthand. In the current ADR, G.22 covers durable chat history and points to A.4, “Durable working memory.” W1 chooses PostgreSQL as the inbound-handoff source of truth for the inbox and outbox. It does **not** decide the durable chat-history backend.

## Rollback boundary

Migration `002` refuses to remove incomplete work. Once all work is completed, its downgrade removes the two new tables and completed history; the existing `audit_event` table remains untouched.
