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
- **Reference backend.** `InMemoryScopeProvider` keeps rows in a dict keyed by (runtime id, subject key), so library mode keeps the contract without Postgres.
- **Postgres backend (extra `postgres`).** `PostgresScopeProvider(engine)` opens each handle transaction with `app.runtime_id` and `app.subject_key` as transaction-local settings (`handle.transaction()` yields the connection). `create_subject_policy(conn, table)` fences a per-user table (columns `runtime_id`, `subject_key`) with a row-level security policy and `FORCE ROW LEVEL SECURITY`, so a query that forgets its filter still returns only the current subject's rows, and a transaction with no scope returns none.
- **The database role matters.** Superusers and roles with `BYPASSRLS` skip row-level security even under FORCE. `PostgresScopeProvider.verify()` refuses such a role. The served app calls it at boot only when you pass `create_app(scope_provider_factory=lambda engine: PostgresScopeProvider(engine))`; without a per-user feature, existing deployments boot on any role.
- **Structural test.** `await assert_subject_isolation(provider, write=..., read_all=...)` (from `agents_system.scope`) writes as two subjects and runs your deliberately unfiltered `read_all` as each; it fails if either sees the other's rows. Every per-user store ships one. Test the Postgres side with a dedicated non-superuser role, never the docker superuser.
