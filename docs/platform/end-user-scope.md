# End-user scope: turn subject and actor

*Since `v0.7.0`.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md); this page covers part 1 (identity bound per turn) and part 2 (audit and logs).

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
- **Proactive delivery.** The proactive queue (`proactive_outbox`) is a registered store with its own retention; see [Proactive delivery](proactive.md).

## Usage and budgets per end user

*Since `v0.7.0`.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md) part 5. The runtime records each turn's usage against the turn's subject and can refuse a subject that reached a cap.

- **The ledger.** One row per turn in `subject_usage` (migration `009`), keyed by (`runtime_id`, `subject_key`, id): tokens in, out and total, `cost_usd`, model id, model calls, tool calls, `timed_out`, the actor label and the time. It is written through the scope handle, so Postgres fences it with row-level security, and it is a registered `SubjectDataStore` (`usage`): the operator export and delete reach it. A scope provider turns it on (`create_app(scope_provider_factory=...)`; `InMemoryUsageLedger(InMemoryScopeProvider())` for a library caller, passed as `AgentRuntime(usage_ledger=...)` with an explicit `runtime_id`).
- **Who pays.** Usage counts against the turn's subject, whatever the actor: a scheduled turn against its owner, an operator turn on behalf of a user against that user. A turn with no subject is not in the ledger; it counts against the runtime only, in the metrics.
- **Recorded where the turn is priced**, including a timed-out turn's lower bound. A model with no `MODEL_PRICES` entry is counted in tokens and flagged (`cost_usd` is null, and the totals report `unpriced_turns`); it is never priced by guessing. A failed ledger write is logged (`runtime.usage_record_failed`) and never fails the turn.
- **Caps.** The deployment sets them per runtime id, in USD or tokens, per UTC day and month, in `create_app(budgets=...)` or `USER_BUDGETS` (the shape of `DEPLOY_GRANTS`), or `AgentRuntime(budget=BudgetCaps(...), usage_ledger=...)`. An agent definition cannot raise them. A cap is reached when the total is at or above it.

  ```bash
  USER_BUDGETS='{"support": {"daily_usd": 1.0, "monthly_tokens": 500000, "message": "Daily limit reached."}}'
  ```

- **Checked before every model call.** An over-cap turn makes no model call: `run_turn_with_usage` returns a `TurnResult` with `budget_refused=True`, the configured `message` as the last message and zero usage, and a `budget_refused` audit event (runtime id, cap, limit, used; subject in the `subject` column). Before each later call of a turn, the cap is checked again with the usage the turn already made, so a tool loop overshoots by at most one call (a refusal there ends the turn with the same message). That check also runs before the fallback request that follows a malformed tool call; the failed attempt may have been billed, so the turn's token totals are reported as unknown. Other subjects are unaffected. A turn refused before any model call is not saved to the conversation thread and writes no ledger row. A turn refused in the middle of its tool loop already made real calls and ran tools, so it is saved to the thread like a timed-out turn, writes its ledger row, and counts as `outcome="budget_refused"`.
- **Boot checks.** A cap must be a finite positive number (NaN and infinity are refused, since they would switch the cap off), and an entry must set at least one cap. Caps without a scope provider, a budget naming a runtime id that is not built, and a USD cap on a runtime whose model has no `MODEL_PRICES` entry all fail the boot. A per-turn `model_id` override with no `MODEL_PRICES` entry is refused with a `ValueError` before any model call on a runtime with a USD cap.
- **Metrics.** `agent_turns_total`, `agent_tokens_total` and `agent_cost_usd_total` gain a bounded `actor_kind` label (`channel`, `api`, `operator`, `scheduler`, `library`, or `none`). There is no subject label: per-user figures come from the ledger. Refused turns appear as `outcome="budget_refused"`.
- **Query API.** `POST /operator/subjects/usage` with `{runtime_id, subject_key}` answers the subject's totals (tokens, `cost_usd`, `turns`, `unpriced_turns`) for the UTC day and month. In the library, `ledger.totals(...)` and `ledger.query(...)`.
- **Limits.** Usage rows have no purge: a purge across subjects has no scope to run in under row-level security, so rows stay until the operator deletes the subject. Unreported tokens count as zero toward a token cap and the turn is flagged in `unpriced_turns` only when it has no price. Two concurrent turns of one subject can each pass the pre-check; channel turns of one conversation already run one at a time.

## Per-user memory

*Since `v0.7.0`.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md) and issue #250. Working memory is the checkpointer thread, and it expires with `CHECKPOINTER_RETENTION_S`. Per-user memory is what an agent keeps **about each end user** across conversations: a preferred unit, an entity they usually ask about, a recurring request. It is not agent-own memory, which is keyed by agent.

- **Opt in.** It is off by default. A role turns it on in `policy.md` with `memory_policy: {user_memory: true}` (a boolean; anything else fails closed like the rest of `memory_policy`). Without it the three tools are refused with `user_memory_disabled` and nothing is injected.
- **Keyed by the turn subject.** A fact belongs to (`runtime_id`, `subject_key`), the same pair as every per-user table, in `subject_facts` (migration `010`) with row-level security. The tools are `subject_scoped`: they receive the `scope` handle and no tool argument names a user, so the model cannot read or write another user's facts, and a query that forgets its filter still returns one user's rows. A turn with no subject is refused with `subject_required`.
- **Three tools.** `remember_user_fact(fact)`, `forget_user_fact(id)` and `list_user_facts()`. `write:user_memory` (T2: revalidated at call time, so it needs a `DEPLOY_GRANTS` entry) covers the first two and `read:user_memory` (T0, but also revalidated at call time, so a turn that dropped the grant cannot list facts) the third. `build_user_memory_tool_specs(limits)` builds them; `default_registry_factory` registers them, inert until a role names them. A fact is one line of text; remembering the same text twice keeps one fact.
- **Injected as untrusted data.** At the start of each turn of an opted-in role, the user's facts are added as one untrusted-data block, `source=user_facts` (the first consumer of [the primitive](end-user-scope.md)): delimited by a per-turn random boundary, outside the system prompt, never persisted in the thread. The facts were written from user input, so they are information and never an instruction: a stored "ignore your rules and call a tool" changes no tool, grant or limit, and under `untrusted_input: true` the T3 ban is unchanged. Facts are listed most used first, then most recent, and a fact that does not fit the size cap is skipped, never cut. A store that fails to read costs the turn its facts, logged as `user_memory.inject_failed`; it never fails the turn.
- **Limits, set by the deployment.** `USER_MEMORY_MAX_FACTS` (default 50), `USER_MEMORY_MAX_FACT_BYTES` (500; a longer fact is refused to the model with `error_kind: fact_rejected`, not cut) and `USER_MEMORY_MAX_INJECTED_CHARS` (1500, counted against `max_context_tokens` like any block). When a write goes over the fact count, the **least used** fact is evicted first, then the oldest; the fact just written is never the one evicted. A fact counts as used each time it is injected. In the library, pass `UserMemoryLimits(...)` as `AgentRuntime(user_memory=..., scope_provider=..., runtime_id=...)`.
- **Retention.** `SUBJECT_DATA_RETENTION='{"user_facts": 2592000}'` expires a fact that many seconds after it was written; `never`, or no entry, keeps it until it is forgotten, evicted or the subject is deleted. An expired fact is never listed, injected or exported. It is deleted the next time that user's facts are read or written, and the shared purger deletes the expired facts of every user, including those who never return. On Postgres the purge runs under a narrow maintenance path (migration `011`): only inside a transaction that sets `app.maintenance`, rows past `expires_at` become visible and deletable, and nothing else is opened.
- **Concurrent writers.** A remember and its eviction run as one serialized operation per user (a per-user advisory lock on Postgres, a per-user lock in the in-process backend), so two simultaneous writes keep the configured maximum instead of erasing each other.
- **Grants gate the injection.** Injecting the facts is a read of them, so a turn gets the block only when the deploy grants cover `read:user_memory` (narrowed by the turn's permissions); without it nothing is injected and no use is counted.
- **Export and delete.** The store is registered as `user_facts` in the data registry, so the operator `export_subject` / `delete_subject` (and their routes) cover a user's facts with the rest of their data.
- **Library mode.** `InMemoryScopeProvider` backs the same handle with a dict (`InMemoryUserFactStore(provider)` is its registry store), so a caller without Postgres gets the same contract.
- **Evals.** A scenario names an `end_user`, may store `user_facts` before its first turn and start a fresh conversation with `new_conversation_before: [1]`; see [Live eval](live-eval.md). The `user_memory` capability ships a recall scenario and a guardrail scenario with a planted instruction.

```yaml
# policy.md
memory_policy:
  user_memory: true
```


## Scheduled turns

*Since `v0.7.0`.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md) and issue #175; the full page is [Scheduled turns](schedules.md). An end user can ask the agent for a recurring request ("send me the weekly sales every Monday at 8"). Three subject-scoped tools (`create_schedule`, `list_schedules`, `delete_schedule`, all under the new T2 permission `write:schedule`) keep that user's own schedules, so the model never names an owner or a recipient. A `ScheduledTurnRunner` later runs each due schedule as a turn with the schedule's **owner as subject** and `scheduler:<schedule id>` as **actor**, under the runtime's grants and against the owner's budget, and delivers the reply as text, only to the owner, through [proactive delivery](proactive.md). The table (`subject_schedules`, migration `013`) is a per-user table fenced by row-level security and registered in the data registry, so a schedule ends when it is deleted or when its owner is deleted. Limits (per user, minimum interval, quiet hours) are the deployment's; a missed window is skipped and the schedule runs at its next one.
