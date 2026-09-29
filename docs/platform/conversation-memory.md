# Conversation memory: the durable checkpointer

A conversation thread is the message history an agent keeps between turns:
what the customer wrote, what the agent answered, and every tool call and
result in between. The served app keeps each thread in PostgreSQL, so a
restart, a `docker compose down` or a container recreation no longer loses a
conversation (issue #24, ADR-002 A.4 and G.22).

| | |
|---|---|
| **Who uses it** | The WhatsApp webhook, and every channel bound through `create_app(channels=...)`, when `WHATSAPP_CHECKPOINTER_ENABLED=true` (the default). The thread id is the customer's normalized address, for WhatsApp the phone number. `POST /v1/chat/completions` stays stateless: its caller sends the whole history. |
| **Where it lives** | The app's own PostgreSQL database (`DATABASE_URL`), in four tables owned by `langgraph-checkpoint-postgres`: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`. |
| **How long** | While the conversation is active, and for `CHECKPOINTER_RETENTION_S` (default 24 hours) after its last turn. A periodic purge deletes it after that. |
| **What a failure does** | The turn is still answered. A thread that cannot be read is answered without its history; a turn that cannot be saved is answered and not remembered. |

## Why PostgreSQL

The platform already chose PostgreSQL for durable state. The webhook inbox
and outbox that make message delivery durable (archive #43, #44 and #46;
Alembic revisions 002 to 004; `services/outbox.py`) and the audit log live
there. Conversation memory reuses that decision instead of deviating from it:

- **One durable store to run, back up and restore.** Redis with AOF and a
  volume would have been a second one, with its own persistence settings,
  backups and failure modes.
- **The same failure domain as the rest of a turn.** A turn already needs the
  database, for its inbox row and its audit events.
- **No special build.** The Redis checkpointer needed Redis 8 with the query
  engine (RediSearch); the Postgres one needs the `postgres:16` the stack
  already runs.

The Redis checkpointer is removed. Redis stays in the stack only for the
`GET /health` probe.

## How a turn uses it

The agent graph itself always runs without a checkpointer. The runtime reads
the thread's history once before the turn and saves the turn's new messages
once after it, as one checkpoint. So:

- **A thread only ever holds whole turns.** A process that dies in the middle
  of a turn leaves the thread exactly where the previous turn ended. The
  durable outbox delivers the message again, and the turn runs again from
  there.
- **A timed-out turn is saved as the customer received it:** the tool calls
  the timeout interrupted are answered with an error, followed by the timeout
  reply. The next turn continues from the conversation the customer saw.
- **The per-turn budget (`total_execution_timeout_s`) bounds the agent's own
  work.** Reading the thread and saving the turn each have their own bound,
  `CHECKPOINTER_TIMEOUT_S`, which the runtime enforces on the client. A
  database that stops answering without refusing anything, such as a frozen
  server or a network partition, is abandoned after that long and handled as
  a failed read or save. It cannot hold the turn, its admission slot or its
  outbox lease. The abandoned call is cancelled in the background.

## The failure policy

The policy depends on where the checkpointer failed, never on which client
library raised the error. It is the same for any LangGraph saver a library
user passes to `AgentRuntime(checkpointer=...)`.

| Where it fails | What the customer gets | What happens to the thread |
|---|---|---|
| Reading the thread: the database is down or does not answer within `CHECKPOINTER_TIMEOUT_S`, the pool is exhausted, or this thread's stored checkpoint cannot be loaded | An answer to this turn, without the earlier history | Nothing is saved. A thread that could not be read is never overwritten with one that lacks its earlier turns. |
| Saving the turn, or the save does not answer within `CHECKPOINTER_TIMEOUT_S` | The finished answer, as it is | It stays at its previous turn. Nothing runs again: no model call is repeated and no tool call is replayed. A save abandoned at its timeout can still complete if the database was already writing it; the thread then holds the turn, whole. |

Both are logged as `runtime.checkpointer_degraded`, with `operation` (`read`
or `write`), `thread_id`, `error_type` and `error`, and counted in
`agent_checkpointer_degraded_total{operation}` (see
`docs/platform/observability.md`). The turn itself still reports `ok`, so
that counter is what shows a checkpointer failing for every turn. The turn's
token usage stays exact, because nothing is ever re-run.

This replaces the policy the Redis checkpointer had. It caught only Redis's
own errors and retried the whole turn without the checkpointer. A save that
failed after a tool had run would run that tool again, the turn's usage was
reported as unknown, and any other backend's error failed the turn.

**One broken thread never fails every turn.** A thread whose stored data is
damaged keeps being answered, without its memory, and no other thread is
affected. It is never written again, so it becomes idle, and the retention
purge deletes it after `CHECKPOINTER_RETENTION_S`. The next message then
starts a fresh thread. To clear it sooner, delete the thread (see
"Deleting one person's conversation" below).

**A dropped connection heals.** The checkpointer has its own connection pool.
A connection broken by a database restart or a network drop is replaced the
next time a turn needs one. While the database is down, turns are answered
without memory rather than failing.

## Retention: a separate, configured value

Durability decides whether a restart loses a conversation. Retention decides
how long an idle one is kept. They are separate settings, and changing where
conversations are stored does not change how long they are kept.

| Setting | Default | Meaning |
|---|---|---|
| `CHECKPOINTER_RETENTION_S` | `86400` (24 h) | How long a conversation is kept after its last turn. `never` keeps conversations indefinitely. Must be positive. |
| `CHECKPOINTER_PURGE_INTERVAL_S` | `3600` | How often the purge runs. It also runs once at startup. |
| `CHECKPOINTER_POOL_SIZE` | `5` | The checkpointer's own connections. They add to the app's other pools against PostgreSQL's `max_connections`. |
| `CHECKPOINTER_TIMEOUT_S` | `5.0` | Upper bound for one thread read and one save, enforced by the runtime. Also bounds taking a connection from the pool, opening one (`connect_timeout`, at least 2 seconds), each statement (`statement_timeout`), and unacknowledged network data (`tcp_user_timeout`). |

The default keeps the 24-hour window the Redis TTL had. It is a platform
default, not a policy: how long a business keeps its customers' conversations
is that business's decision, so set it explicitly.

The purge runs inside the app process:

1. It deletes every thread whose last turn is older than the retention: all
   of its rows, in all three tables.
2. It prunes every thread down to its latest checkpoint. Each save writes a
   full copy of the history, and the platform never reads an older one, so
   keeping them would grow storage with the square of a conversation's
   length. The latest checkpoint still holds the whole history.

A conversation idle for longer than the retention is therefore deleted within
`CHECKPOINTER_RETENTION_S + CHECKPOINTER_PURGE_INTERVAL_S`. Each run logs
`checkpointer.purged` with counts only (`threads_deleted`,
`checkpoints_pruned`, `retention_s`), never a thread id. A failed run logs
`checkpointer.purge_failed` with `consecutive_failures`, and the next interval
tries again. Every run is counted in
`agent_checkpoint_purges_total{outcome}`: runs that keep failing mean
conversations outlive their retention, so alert on them.

## Personal data

A thread is personal data. It holds every message the customer sent and every
reply, tool results such as order details, names or addresses, and its key is
the customer's phone number.

- **What the window means.** A conversation is kept whole while it is active,
  and for `CHECKPOINTER_RETENTION_S` after its last turn, plus up to one purge
  interval. A customer who writes every day keeps one growing thread.
- **`never` is a deliberate choice.** Keeping conversations indefinitely
  needs a documented reason and legal basis in the deployment that sets it.
- **Durable now means durable in backups too.** Redis lost conversations on
  restart. PostgreSQL keeps them, so they also reach the database's backups,
  WAL archives and replicas, under those systems' own retention. Review the
  backup retention when you set `CHECKPOINTER_RETENTION_S`: a purged
  conversation remains in a backup taken before the purge.
- **Deleted means a SQL `DELETE`.** The space is reclaimed by PostgreSQL's
  vacuum. Logs are separate: `runtime.checkpointer_degraded` names the thread
  id, so the log retention also applies to phone numbers.
- **Other stores keep their own retention.** The audit log (`audit_event`,
  see `docs/platform/audit.md`) and a deployment's conversation recorder are
  separate stores. Deleting a thread does not touch them.

### Deleting one person's conversation

For an erasure request, delete the thread by its normalized address, for
example `+5491100000000`:

```sql
DELETE FROM checkpoint_writes WHERE thread_id = '+5491100000000';
DELETE FROM checkpoint_blobs  WHERE thread_id = '+5491100000000';
DELETE FROM checkpoints       WHERE thread_id = '+5491100000000';
```

From Python, `await saver.adelete_thread("+5491100000000")` does the same.
For a channel other than WhatsApp, the thread id is prefixed with the
channel's name (`services.outbox.channel_scoped_key`).

## The connection

The checkpointer opens its own connections (psycopg, through libpq) from the
same `DATABASE_URL` as the rest of the app, so there is no second URL to keep
in sync. The query parameters of that URL are read like this:

| In `DATABASE_URL` | For the checkpointer |
|---|---|
| `ssl=<mode>`, asyncpg's name | Becomes libpq's `sslmode`. The mode is one of `disable`, `allow`, `prefer`, `require`, `verify-ca` or `verify-full`; asyncpg's `verify_ca` and `verify_full` spellings work too. `ssl=true` is refused, as the app's asyncpg engine also refuses it. |
| `prepared_statement_cache_size`, `command_timeout`, `async_fallback` | Left out. They tune the app's own asyncpg engine. The checkpointer prepares no statements and has its own timeouts. |
| Any other libpq connection parameter, such as `target_session_attrs` or `passfile` | Passed through. |
| Anything else | Refused at boot, and by `alembic upgrade head`, with an error that names the parameter. |

## Schema and migrations

The tables belong to `langgraph-checkpoint-postgres`, which versions its own
schema in `checkpoint_migrations`. Alembic revision 007 creates them by
running the saver's own migrations, not a copy of their DDL. So:

- the tables are created in the deployment's migration step
  (`alembic upgrade head`), by the migration role, like every other table;
- the app never runs DDL at boot, and refuses to start when the schema is
  missing or older than the installed saver needs: the error says to run
  `alembic upgrade head`;
- the app also refuses to start when its own database role cannot keep
  threads in those tables. When the app connects as a different role than
  the migration role, grant it `SELECT, INSERT, UPDATE, DELETE` on
  `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`, and `SELECT` on
  `checkpoint_migrations`. A role that could only read would boot, then
  forget every conversation and never purge one. A read-only connection
  (`default_transaction_read_only`, or a standby server) is refused for the
  same reason. The error names each missing privilege;
- `alembic revision --autogenerate` ignores these tables (`alembic/env.py`).

When an upgraded `langgraph-checkpoint-postgres` ships new migrations, a
database already at revision 007 does not get them. The offline suite fails
first (`tests/test_checkpointer_service.py`): add a new Alembic revision that
calls `services.checkpointer.apply_checkpoint_migrations`, then raise
`CHECKPOINT_SCHEMA_VERSION`.

Downgrading below 007 drops the four tables and every conversation with them.

## Upgrading from the Redis checkpointer

1. **Deploy the new version and run `alembic upgrade head`.** The migrate step
   of `docker-compose.prod.yml` and the systemd migration unit already do
   this.
2. **Rename `CHECKPOINTER_TTL_S` to `CHECKPOINTER_RETENTION_S`.** The old name
   is still read, as the retention it always was, and the boot logs
   `checkpointer.deprecated_setting`. Setting both is refused at boot.
3. **Expect one fresh start per conversation.** Threads held in Redis are not
   copied. Each customer's next message starts a new thread. Under the old
   24-hour TTL, those threads would have expired within a day anyway. Once the
   new version runs, the old Redis keys can be deleted or left to expire.
4. **Check the connection budget.** The checkpointer's pool adds up to
   `CHECKPOINTER_POOL_SIZE` connections per process (see
   `docs/operations/deployment.md`).

Rolling back to the previous version: its code uses Redis again. The
conversations saved in PostgreSQL stay in their tables until
`alembic downgrade 006` drops them.

**Library users.** The `server` extra no longer installs
`langgraph-checkpoint-redis`; the Postgres checkpointer comes with the
`postgres` extra, which `server` includes. `AgentRuntime(checkpointer=...)`
accepts any LangGraph saver, and the failure policy above applies to all of
them. Each read and save is bounded by
`AgentRuntime(checkpointer_timeout_s=...)`, 5 seconds by default. To keep
threads in Redis, install that package yourself and pass its saver. To use PostgreSQL outside the served app,
`services.checkpointer.open_postgres_checkpointer(database_url, pool_size=...,
timeout_s=...)` opens the same pooled saver. Create its tables with
`apply_checkpoint_migrations(database_url)` or the saver's own `setup()`.

## Evidence

- `tests/test_checkpointer_failure_policy.py`: the failure policy with plain
  exceptions and no backend (a failed read, a failed save, one broken
  thread, one save per turn, a timed-out turn, a checkpoint that decodes into
  something that is not a message list, a read or save that never answers,
  and a driver slow to cancel that does not hold the turn).
- `tests/test_checkpointer_service.py`: the connection string and the
  `DATABASE_URL` options it keeps, drops or refuses, the connection timeouts, what the app refuses to boot on, the schema version
  tripwire, the purge loop, and the absence of a `DeltaChannel` that
  would make pruning unsafe.
- `tests/test_checkpointer_integration.py`, against a real PostgreSQL (CI job
  `checkpointer-integration`, `CHECKPOINTER_TEST_DATABASE_URL`): revision 007
  and its downgrade, a refused unmigrated database, a conversation that
  survives a process restart (each turn in its own Python process), a role
  that cannot save or purge threads and a read-only role both refused, the purge
  of idle threads and of superseded checkpoints, a corrupt thread that
  degrades alone, and a dropped connection that heals on the next turn.
