# End-user scope: turn subject and actor

*Coming in the next release.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md); this page covers part 1 (identity bound per turn) and part 2 (audit and logs).

A deployed agent serves many end users through one cached runtime. Each turn carries two values the entry point binds and the model never sees:

| Value | Meaning | Shape |
|---|---|---|
| `TurnSubject` | The end user the turn is for | `key` (`participant:<id>`), plus `channel` and `address` for delivery only |
| `TurnActor` | Who started the turn | `kind` (`channel`, `api`, `operator`, `scheduler`, `library`) and `id`; its label is `<kind>:<id>` |

```python
from agents_system import TurnActor, TurnSubject

subject = TurnSubject.for_participant(42, channel="whatsapp", address="+5491100000000")
await runtime.run_turn(
    messages,
    session_id="s1",
    subject=subject,
    actor=TurnActor(kind="library", id="job"),
)
```

Both are keyword-only and default to `None`; a turn without them behaves as before. The key uses the participant id, not the address, because an address can be reassigned to another person.

## Who binds what

| Turn | Subject | Actor |
|---|---|---|
| Inbound channel turn | The participant the worker resolves | `channel:<channel>` |
| API turn | Only from a `SubjectResolver`; otherwise none | `api:<runtime id>` |
| Library call | Whatever the caller passes | Whatever the caller passes |

The OpenAI-compatible adapter authenticates one shared key, so it knows no end user. A deployment that does supplies a resolver:

```python
class MyResolver:
    async def resolve(self, request):  # -> Participant | None
        ...


app = create_app(registry_factory=..., subject_resolver=MyResolver())
```

`None` means the request has no end user and the turn runs with no subject. An inactive participant is refused with 403 and no turn runs. The request's `user` field and message text are never a subject.

## What it reaches

- **Audit:** every event of the turn fills `actor` (`<kind>:<id>`) and the new nullable `subject` column (migration `008`) with the subject key. The address never goes in a column. See [Audit](audit.md).
- **Turn logs:** `runtime.turn_usage` and every log line of the turn carry `subject` (the key) and `actor`, never the address.
- **Participant ids:** a `Participant.id` must be unique across every channel the directory serves. See [Channel](channel.md).

## Subject-scoped tools and the scope handle

A tool never holds a subject key. Mark a tool `ToolSpec(..., subject_scoped=True)` and its **async** connector receives a `scope=` keyword, a handle bound to (runtime id, subject key) that the interceptor builds from the turn's subject. The handle's methods take neither value; the model never sees or passes them.

```python
from agents_system.scope import InMemoryScopeProvider

async def remember(inputs, *, session=None, scope):
    scope.put("notes", inputs["id"], inputs["text"])      # reference backend
    return {"notes": scope.rows("notes")}                  # only this subject's rows

ToolSpec(name="remember", ..., connector=remember, subject_scoped=True)
runtime = AgentRuntime(equipped, scope_provider=InMemoryScopeProvider(), runtime_id="support")
```

- **No subject, no run.** A subject-scoped tool called in a turn with no subject, with no `scope_provider`, or with a sync connector is refused (`subject_required`, `scope_unavailable`, `scope_requires_async_connector`) and audited as a blocked tool call. It never runs against a default.
- **Explicit runtime id.** An `AgentRuntime` given a `scope_provider` (any per-user store) and no `runtime_id` raises `ValueError`: the fallback is the model's name, which two agents share. Treat a runtime id as permanent once per-user features are on; renaming it orphans the data.
- **`memory_policy` gates it (#26).** A subject-scoped tool that reads or writes agent memory declares `memory_access` (`read`, `write` or `readwrite`) and `memory_scope` on its `ToolSpec`. The interceptor checks them against the role's `memory_policy` before the connector runs, and refuses the call, audited, with `memory_read_denied`, `memory_write_denied` or `memory_policy_invalid`. See [Role definition](role.md#enforcement).
- **Reference backend.** `InMemoryScopeProvider` keeps rows in a dict keyed by (runtime id, subject key), so library mode keeps the contract without Postgres.
- **Postgres backend (extra `postgres`).** `PostgresScopeProvider(engine)` opens each handle transaction with `app.runtime_id` and `app.subject_key` as transaction-local settings (`handle.transaction()` yields the connection). `create_subject_policy(conn, table)` fences a per-user table (columns `runtime_id`, `subject_key`) with a row-level security policy and `FORCE ROW LEVEL SECURITY`, installed as both a permissive and a restrictive policy (a restrictive policy is AND-combined, so a pre-existing permissive policy on the table cannot widen it), so a query that forgets its filter still returns only the current subject's rows, and a transaction with no scope returns none.
- **The database role matters.** Superusers and roles with `BYPASSRLS` skip row-level security even under FORCE. `PostgresScopeProvider.verify()` refuses such a role. The served app calls it at boot only when you pass `create_app(scope_provider_factory=lambda engine: PostgresScopeProvider(engine))`; without a per-user feature, existing deployments boot on any role.
- **Structural test.** `await assert_subject_isolation(provider, write=..., read_all=...)` (from `agents_system.scope`) writes as two subjects and runs your deliberately unfiltered `read_all` as each; it fails if either sees the other's rows. Every per-user store ships one. Test the Postgres side with a dedicated non-superuser role, never the docker superuser.

## Per-user data: registry, export, delete and retention

Every store that keeps data per end user implements `SubjectDataStore` and registers it, so one operator call reaches all of them and a store cannot be forgotten.

```python
from agents_system.subject_data import DEFAULT_REGISTRY, SubjectRef


class NotesStore:
    name = "notes"
    tables = frozenset({"notes"})  # every table with a subject_key column

    async def export(self, subject: SubjectRef) -> list[dict]: ...
    async def delete(self, subject: SubjectRef) -> int: ...
    async def purge(
        self, *, retention_s: int
    ) -> int: ...  # optional: rows older than the retention


DEFAULT_REGISTRY.register(NotesStore())  # or create_app(subject_data=registry)
```

- **Library.** `export_subject(registry, SubjectRef(runtime_id, subject_key), threads=..., thread_ids=[...])` and `delete_subject(..., actor=TurnActor(kind="operator", id="ops"))`. `delete_subject` attempts every store and every thread even when one fails, then raises `SubjectDeletionError` carrying the failed stores, the failed thread count and the counts that did delete.
- **Conversation threads.** Checkpointer threads are keyed by conversation, not by subject, so they are named: the deployment passes `create_app(subject_threads=async_fn)` (subject reference to thread ids) and it is the only authority on which threads belong to a subject. An operator may name `thread_ids` in the request to narrow the set, but a named id the hook does not return for that subject is refused with 403, and without the hook the routes fail closed with 409 while a checkpointer exists. Export returns every retained checkpoint (all channel values, metadata and pending writes), the same data the delete erases. `CheckpointThreads(saver)` exports and deletes one thread on demand; the purger still removes idle threads by age.
- **Nothing forgotten.** `await find_unregistered_subject_tables(engine, registry)` (Postgres, from `agents_system.services.scope_postgres`) returns the tables with a `subject_key` column that no registered store claims. Assert it is empty in your store's test.
- **Operator routes.** Set `OPERATOR_API_KEY` and the served app mounts `POST /operator/subjects/export` and `POST /operator/subjects/delete`. Without the key the routes do not exist (404). The key must differ from `ADAPTER_API_KEY` and `METRICS_API_KEY`, and is checked with the same constant-time comparison as the adapter's.

  ```bash
  curl -X POST $HOST/operator/subjects/delete \
    -H "Authorization: Bearer $OPERATOR_API_KEY" -H "X-Operator-Id: ops-ana" \
    -H "Content-Type: application/json" \
    -d '{"runtime_id": "support", "subject_key": "participant:42"}'
  ```

  The subject travels in the body, so it never lands in an access log. A partial failure answers 500 with the failed stores and what was deleted.
- **Audit.** A deletion records `subject_data_deleted` with the operator as actor (`operator:<X-Operator-Id>`, default `operator:operator`). The result carries `audited`: it is `false`, with a warning logged, when no audit sink is registered or the write failed; the deletion itself is never undone or hidden. The served app's sink is fire-and-forget, so the event is best effort. Audit rows keep only the pseudonymous subject key and stay under their own retention. The consumer's `ConversationRecorder` belongs to the deployment, which deletes from it.
- **Retention.** `SUBJECT_DATA_RETENTION='{"notes": 2592000, "usage": "never"}'` sets one retention per store, in seconds or `never`, parsed like `CHECKPOINTER_RETENTION_S`; a store not listed keeps its rows. One `SubjectDataPurger` runs every `SUBJECT_DATA_PURGE_INTERVAL_S` seconds (default 3600), logs a failing store and continues with the rest. A name no store registered fails the boot.
- **A runtime id is permanent** once per-user features are on: rows are keyed by it, so renaming it orphans them and the operator calls no longer find them.
