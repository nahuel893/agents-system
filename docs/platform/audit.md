# Audit Trail

`docs/platform/harness.md` states *what* must be reconstructible from the audit
trail. This document describes *how* that is implemented: the emit path, the
redaction policy, the sink, and the partitioned store behind it.

The subsystem has one non-negotiable property: **auditing never breaks the
request it is auditing.** Every emit point is fire-and-forget, every failure is
swallowed at the boundary — and, because a swallowed failure that leaves no
trace is indistinguishable from a feature that does nothing, every swallow is
logged.

## Pipeline

```
Harness call site (sync or async)
  → _emit(recorder_name, **kwargs)        schedules onto the running loop
    → _emit_async()                       resolves the recorder, catches everything
      → recorder.record_*()               builds the typed event
        → Redactor.redact()               strips PII, returns (payload, pii_keys)
          → AuditSink.record()            put_nowait onto a bounded queue — returns immediately
            → _drainer_loop()             batches
              → _flush_batch()            one INSERT per batch
                → audit_event_YYYY_MM     PostgreSQL, RANGE partitioned on occurred_at
```

The caller's thread of control ends at `record()`. Everything past the queue
happens on the drainer task.

### Why `_emit` is synchronous

`_emit_async` is a coroutine. Three of the four modules that emit
(`resolve_tool_surface`, `build_runtime`, `_load_skills`) are synchronous
functions and cannot await it. Calling a coroutine function without `await`
builds a coroutine object and discards it — no exception, no log, only a
`RuntimeWarning` that does not fail a test run.

So `_emit` is a plain `def` that schedules the coroutine on the running loop and
**retains a strong reference to the task**. `asyncio.create_task` holds only a
weak reference; without the retaining set, a task can be garbage-collected
mid-flight. With no running loop at all — synchronous unit tests, CLI entry
points — it logs `audit.emit_skipped_no_loop` and returns. That is not an error.

Never call `_emit_async` directly from a call site. Use `_emit`.

## Events

Ten event types, modelled as a Pydantic v2 discriminated union over
`event_type`. All share a common base:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | `UUID` | Unique per event |
| `occurred_at` | `datetime` | UTC. Also the partition key |
| `correlation_id` | `str` | Groups every event from one execution |
| `sequence` | `int` | Monotonic within a `correlation_id` |
| `role` | `str` | The agent definition that was active |
| `deployment` | `str \| None` | Which deployment narrowed that role |
| `actor` | `str \| None` | Trigger identity |
| `payload` | `dict` | Event-specific data, stored as JSONB |
| `pii_keys` | `list[str]` | Top-level payload keys whose values were redacted |

`pii_keys` is what makes redaction auditable: the record states which fields
were stripped, so an operator can tell "no phone number was present" apart from
"a phone number was present and removed".

The ORM row is not a one-to-one mirror of this model. Four fields are promoted
out of `payload` into real columns — `event_type`, `tool_name`,
`policy_decision`, `policy_reason` — because they are what an incident query
filters on, and filtering JSONB is slower than filtering a column. Everything
else stays in `payload`.

### Event types and where they are emitted

| Event | Emitted from | Meaning |
|---|---|---|
| `tool_granted` | `harness/injector.py` | A tool entered the injected surface (Layer 1) |
| `tool_denied` | `harness/injector.py` | A tool was withheld from the surface |
| `skill_loaded` | `harness/factory.py` | A skill was attached to the runtime |
| `skill_missing` | `harness/factory.py` | A named skill could not be resolved |
| `runtime_built` | `harness/factory.py` | An `EquippedRuntime` was assembled |
| `runtime_initialized` | `agent/graph.py` | The graph accepted a turn |
| `runtime_timeout` | `agent/graph.py` | The turn exceeded `total_execution_timeout_s` |
| `unknown_tool` | `harness/injector.py` | A role named a tool that is not in the registry |
| `tool_call_attempted` | `harness/interceptor.py` | A call reached Layer 2 |
| `tool_call_blocked` | `harness/interceptor.py` | Layer 2 refused the call |

Twelve call sites for ten event types. `tool_call_blocked` is emitted from three
distinct branches of the interceptor — `not_in_surface`, `revalidation_required`,
and `permission_revoked` — which is why the counts differ.

Note that `unknown_tool` is a **build-time** event. It fires in the injector when
a role manifest names a tool the registry does not have, immediately before
`InjectionError` is raised; the runtime never starts. It is not the
interceptor's `not_in_surface` case, which is a *model* naming a tool at call
time against a runtime that built successfully.

## Redaction

`Redactor.redact(payload, audit_policy)` returns `(redacted_payload, pii_keys)`.
The policy is **default-deny**: a value is kept only if it is known to be safe.

| Category | Behaviour | Marker |
|---|---|---|
| Phone numbers | Always redacted, detected via `phonenumbers` | `[REDACTED:phone]` |
| Email addresses | Always redacted, regex-detected | `[REDACTED:email]` |
| Free-text keys (`message`, `body`, `text`, `email`) | Redacted **unless** `audit_policy.capture_tool_input` is `True` | `[REDACTED:body]` |
| Keys named in `audit_policy.redact_keys` | Always redacted | `[REDACTED:custom]` |

Phones and emails are stripped even when `capture_tool_input` is enabled —
opting into free-text capture is not opting into storing customer identifiers.

Detection runs over *values*, not only key names, so a phone number embedded in
a driver error message or a policy-denial reason is caught. This matters: the
audit payload is the one place where an exception string from an external
connector is persisted verbatim.

## Sink

`AuditSink` is a process-wide singleton (`AuditSink.current()`), constructed in
the FastAPI lifespan and handed the application's `AsyncEngine`-backed session
factory.

| Property | Behaviour |
|---|---|
| `record(event)` | `put_nowait` onto a bounded queue. Returns within ~5 ms. **Never raises.** |
| Queue full | Increments `dropped_count`, logs `audit.event_dropped`, drops the event |
| `record()` before `start()` | Raises `RuntimeError` — a misconfiguration, not a runtime condition |
| `record()` after the sink closed | Counted in `dropped_count`, logs `audit.event_dropped_shutdown`, never raises. The sink closes at the drainer's final sweep; until then — even while `stop()` waits — events are still accepted and written |
| Drainer | Batches events and issues one INSERT per batch |
| `drain()` | Waits for the queue to empty. Does **not** cover the drainer's in-flight batch |

Dropping under pressure is deliberate. The alternative — blocking the caller
until the audit write completes — makes auditing a latency dependency of the
request path, which is exactly what the fire-and-forget contract exists to
prevent. `dropped_count` and the `audit.event_dropped` warning are the signal
that the queue size needs raising.

### Shutdown drains the tail (fixed — #8)

`stop()` used to set `_shutdown` and then immediately cancel the drainer task.
The drainer's shutdown flush lives *after* its `while not self._shutdown` loop,
but the cancellation raised `CancelledError` inside the awaited `queue.get()`,
so that flush was never reached — every event still queued, plus the drainer's
in-flight batch, was discarded on graceful shutdown.

`stop(timeout: float = 5.0)` now **awaits** the drainer task instead of
cancelling it, which is what lets its shutdown-drain tail actually run and
flush whatever was left. The `timeout` bounds this so a wedged drainer (e.g. a
stuck DB call) cannot hang shutdown forever: on timeout, `asyncio.wait_for`
cancels the drainer as a last resort and `stop()` logs
`audit.shutdown_timeout`. The default is backward compatible with every
existing no-arg `await sink.stop()` call site.

**Every event handed to the sink is written or counted** (PR #72 review).
`dropped_count` is the only aggregate loss signal (ADR-001 D-046), so:

- `stop()` counts the batch a cancelled drainer was flushing
  (`in_flight_events_dropped`) and whatever is still queued because no
  drainer swept it (`queued_events_dropped`) — after a timeout, or when the
  drainer died before `stop()` (then logged as
  `audit.shutdown_events_dropped`). After a clean exit both are zero and
  nothing is logged.
- The drainer closes the sink immediately before its final sweep, with no
  `await` in between: every `record()` accepted before that point is in the
  final batch, and every later one is rejected and counted
  (`audit.event_dropped_shutdown`) instead of sitting in a queue nobody reads.
- A failed flush (`audit.drain_failed`) adds its whole batch.

This was pinned as a `strict=True` xfail in `tests/test_audit_wiring.py`
(`test_stop_does_not_lose_queued_events`); the marker is now removed and the
test passes for real.

## Storage

`audit_event` is RANGE partitioned on `occurred_at`, monthly, plus a `DEFAULT`
partition. Two consequences worth knowing before touching the model:

**Every key is composite.** PostgreSQL requires the partition key in every
`PRIMARY KEY` and `UNIQUE` constraint on a partitioned table, which reshapes both:

| Constraint | Columns |
|---|---|
| `pk_audit_event` | `(occurred_at, id)` — `id` is `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `uq_audit_event_correlation_sequence` | `(occurred_at, correlation_id, sequence)` |

`event_id` (the UUID carried on the Pydantic event) is therefore *not* a primary
key column — it holds its own `UNIQUE` constraint instead. Ordering within an
execution comes from `(correlation_id, sequence)`, not from `id`.

SQLite cannot express autoincrement on a composite primary key, which is the
proximate reason no SQLite fixture may create this table.

**The ORM must not create it.** SQLAlchemy cannot express partitioning. On
SQLite it raises; on PostgreSQL it silently produces a plain unpartitioned table
that differs from what the migration builds — which is worse, because nothing
reports it. So the table declares ownership and creation skips it:

```python
class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = {"info": {ALEMBIC_OWNED: True}}
```

`alembic_owned_tables()` discovers that flag; nothing in `Base.metadata` may be
bulk-created from ORM metadata if it appears there. Discovery is by flag, not
by a name list, so a second partitioned table inherits the behaviour without a
code change. `audit_event` is currently the platform's only ORM-declared
table (#70 removed the client-owned ones and the pgvector stack), so there is
nothing left for the ORM to create at all — `alembic upgrade head` owns
every table.

### The `DEFAULT` partition is an availability guard, not a convenience

Without it, an insert whose `occurred_at` falls outside every declared partition
is rejected outright:

```
ERROR:  no partition of relation "audit_event" found for row
```

That would stop auditing on a calendar date, with no code change and nothing in
a diff to blame, making the monthly partition job a hard availability
dependency. With the `DEFAULT` partition, rows always land, and adding monthly
partitions is what it should be: an optimization for query pruning and
retention.

Partitions are named `audit_event_YYYY_MM`, computed from the same bounds as
their range, with explicit UTC boundaries. `occurred_at` is `TIMESTAMPTZ`, and a
bare date literal is interpreted in the session's `TimeZone` — which would split
the month differently depending on who ran the migration.

## Adding a new event type

1. Add the sub-model to `src/agents_system/audit/events.py` and register it in the
   dispatch map. Fields go in `payload`, not on the sub-model, unless they need
   to be indexed.
2. Add a `record_*` coroutine to `src/agents_system/audit/recorder.py`. It must build
   the payload and pass it through `_build_and_redact` — never construct the
   event directly, or PII bypasses the redactor.
3. Call it from the harness through `_emit("record_your_event", ...)`. Never
   `_emit_async`.
4. Assert **delivery**, not invocation. A test that checks `_emit` was called
   passes against a discarded coroutine. Drive a real caller and assert the
   event reaches `_flush_batch`; `tests/test_audit_wiring.py` has the
   `CapturingSink` pattern for this.

## Sequencing

`sequence` is monotonic within a `correlation_id`, and `(occurred_at,
correlation_id, sequence)` is a UNIQUE constraint — so the counter is what
guarantees an execution's events can be ordered after the fact, and a duplicate
fails the INSERT for the whole batch.

### The sequence is allocated in the database (fixed — #9)

The allocator used to be a module-level dict behind an `asyncio.Lock` in
`audit/recorder.py` — correct within one process, wrong across N: every
process counted the `"none"` fallback `correlation_id` (used whenever there is
no bound request context) from 1 independently, so two workers emitting a
contextless event in the same instant could allocate the same sequence and
violate the UNIQUE, losing the whole batch. The dict was also never pruned,
growing one entry per `correlation_id` for the life of the process.

`_allocate_sequence` and `_seq_counter` are gone. Each `record_*` helper now
writes `PLACEHOLDER_SEQUENCE` (`0`) — a value that is never persisted — and
the real, authoritative sequence is reserved atomically at flush time, in
`AuditSink._flush_batch`: one upsert per **distinct** `correlation_id` in the
batch, in **sorted** order, each reserving that correlation's whole
contiguous range at once:

```sql
INSERT INTO audit_sequence (correlation_id, next_seq)
VALUES (:correlation_id, :count)
ON CONFLICT (correlation_id)
DO UPDATE SET next_seq = audit_sequence.next_seq + EXCLUDED.next_seq
RETURNING next_seq
```

`audit_sequence` (migration `005_audit_sequence`) is a small, unpartitioned
table — one row per `correlation_id`. `next_seq` is the **last** value handed
out, so a result `r` means the batch owns `r - count + 1 .. r`, handed out in
queue order so one correlation's events keep their order. The upsert runs on
the SAME session the batch's events are added to and committed with, so a
failed batch rolls its reservation back for free. What makes this survive N
worker processes, which the in-process counter never could: PostgreSQL's own
row lock on each `audit_sequence` row serializes concurrent writers from
*any* process, not just coroutines sharing one.

### Why sorted, and one upsert per correlation (PR #72 review)

Each upsert's row lock is held until the batch commits, so the order in which
a flush issues its upserts is the order in which it takes locks. The first
version issued one per event, in queue order: a worker holding `c1` and
waiting for `c2` while another held `c2` and waited for `c1` deadlocked,
PostgreSQL aborted one transaction, and the drainer dropped that whole batch
(18 of 20 runs in a probe). Every flush in every process now locks in one
global order — sorted `correlation_id` — so a later flush queues behind an
earlier one instead of closing a cycle. One upsert per correlation also locks
each row once per flush, and costs one round trip per distinct correlation
instead of one per event.

`tests/test_audit_migration_integration.py::TestAuditSequenceAllocationIsProcessSafe`
proves this against a real PostgreSQL instance, through separate engines:
concurrent flushes of one correlation stay gap-free and duplicate-free;
opposite-order batches, parked by a gate until each holds its first lock,
deadlock on every run without the sort and never with it; and shuffled
multi-correlation batches from three workers lose nothing and keep queue
order. A mock-based test cannot: mocks share one process's memory and take no
row locks.

### Migration 005 seeds the counters

`upgrade()` seeds `audit_sequence` with `MAX(sequence)` per `correlation_id`
already in `audit_event`, so no counter restarts at 1 under numbers history
already used — the shared `"none"` fallback above all. `MAX`, not `count(*)`:
the old per-process counters restarted on every boot, so history has gaps
and repeats.

The cost: no index leads with `correlation_id`, so the seed is a sequential
scan of every partition, `DEFAULT` included, inside the migration's
transaction, and it writes one `audit_sequence` row per distinct historical
correlation_id. It takes only `ACCESS SHARE`, so concurrent inserts are not
blocked, but on a large `audit_event` the upgrade lasts as long as a
full-table read — run it in a maintenance window. Restart every worker
afterwards: code from before 005 still numbers from its in-process counter.

### A second, independent writer: the outbox

`services/outbox.py:_terminalize_work` also writes `audit_event` — the
`webhook_delivery_terminal_failure` event — directly on its own session, with
`sequence = attempt_count` and `correlation_id = "outbox:<work id>"`. It does
not go through `audit_sequence`, deliberately:

- **The namespaces cannot overlap.** Everything the sink writes carries the
  request id (`uuid4().hex[:8]`, bound by `observability/middleware.py`:
  eight hex characters, never a colon) or `"none"`. Neither can equal
  `"outbox:<uuid>"`.
- **Its own namespace cannot collide.** It runs only on the terminal
  transition, under the `FOR UPDATE` lock of the live-lease row, and it sets
  `failed_at`, which excludes that row from every later lock — so each
  `"outbox:<id>"` gets exactly one event, and `attempt_count` is the
  meaningful number to store.
- **Routing it through the allocator** would add an `audit_sequence` row per
  failed work item and a second row lock to the outbox's terminal
  transaction, to prevent a collision that cannot happen.

The invariant to keep: a writer that shares a correlation_id with any other
writer must reserve through `audit_sequence` — including `_terminalize_work`,
the day anything else writes under `"outbox:*"`.

## Operational signals

| Log event | Meaning | Action |
|---|---|---|
| `audit.event_dropped` | Queue full, event lost | Raise `maxsize`, or investigate drainer stalls |
| `audit.emit_failed` | The emit path raised | Read `exc_info` — the request itself was unaffected |
| `audit.drain_failed` | A batch INSERT failed; those events are gone | Read `error` and `exc_info`. Carries `batch_size`; the batch is added to `dropped_count` |
| `audit.emit_skipped_no_loop` | No running event loop | Expected in sync tests and CLI entry points |
| `audit.shutdown_timeout` | `stop()`'s `timeout` elapsed before the drainer exited on its own; it was cancelled as a last resort | Carries `queued_events_dropped` and `in_flight_events_dropped`, both added to `dropped_count`. Investigate what wedged the drainer (usually a stuck DB call); raise `timeout` only if the drainer is simply slow, not stuck |
| `audit.shutdown_events_dropped` | `stop()` found events no drainer would flush — the drainer had died before `stop()` | Same fields as above. Find out why the drainer task ended early |
| `audit.event_dropped_shutdown` | `record()` after the sink closed; the event was counted, not written | Something emits audit events after shutdown began — find the caller (usually a background task outliving the lifespan) |

## Implementation

- `src/agents_system/audit/events.py` — the discriminated union
- `src/agents_system/audit/redactor.py` — default-deny PII policy
- `src/agents_system/audit/recorder.py` — event builders, one per type
- `src/agents_system/audit/sink.py` — queue, drainer, batched writes
- `src/agents_system/harness/injector.py` — `_emit` / `_emit_async`
- `src/agents_system/models/audit_event.py` — ORM model, Alembic-owned
- `alembic/versions/` — table, partitions, and the `DEFAULT` partition
- `tests/test_audit_wiring.py` — end-to-end delivery, mutation-verified
- `tests/test_audit_sink.py`, `tests/test_audit_redactor.py`,
  `tests/test_audit_recorder.py` — unit coverage

Partition behaviour is covered by integration tests against real PostgreSQL in
the `audit-migration` CI job. It cannot be unit tested: the ORM model carries no
partitions, so no in-memory test can tell whether a row landed in one.

## Cross-references

- Audit trail requirements: `docs/platform/harness.md` (Audit trail section)
- Layer 2 enforcement, which emits four of the ten events: `docs/platform/interceptor.md`
- `audit_policy` fields (`retention_days`, `capture_tool_input`, `redact_keys`): `docs/platform/policy.md`
- Permission model and RBAC: `docs/architecture/permission-model.md`
