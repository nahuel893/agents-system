# Scheduled turns

*Since `v0.7.0`.* An end user asks the agent over a channel: "send me the weekly sales every Monday at 8". The agent creates a schedule for that user; later the platform runs it as a turn **for that user** and delivers the answer to them. Design: [ADR-011](../architecture/adr-011-end-user-scope.md) (turn subject and actor, scope handle, proactive delivery); issue #175.

The model never chooses who the schedule is for or who receives it. Both come from the platform: the owner is the turn's subject, and delivery goes only to that owner through [proactive delivery](proactive.md).

## The tools

Three subject-scoped tools (see [End-user scope](end-user-scope.md#subject-scoped-tools-and-the-scope-handle)), so each receives a handle already bound to the current end user and holds no user id:

| Tool | Does |
|---|---|
| `create_schedule(prompt, frequency, time?, weekday?, every_hours?, timezone?)` | Stores a recurring request for this user |
| `list_schedules()` | Lists this user's schedules, with ids and next run |
| `delete_schedule(id)` | Deletes one of this user's schedules |

`prompt` is the request as it should be asked later, in the user's words; the conversation will be gone by then. `frequency` is `daily`, `weekly` (with a `weekday`, `monday` to `sunday`) or `interval` (`every_hours`); `time` is a 24-hour `HH:MM` wall-clock time in `timezone` (an IANA name, default `SCHEDULE_TIMEZONE`). There is no recipient, address or user argument.

All three need the new **`write:schedule`** permission. It is T2: revalidated at call time, so the deployment must list it in `DEPLOY_GRANTS`, and the tools are not even equipped without it. Register them with `build_schedule_tool_specs(limits)`; the served app's default registry already does, so a role only has to name them in its `tools:`. A refused call returns an `error_kind` the model can report: `invalid_prompt`, `invalid_schedule`, `interval_too_short`, `quiet_hours`, `limit_reached`, `scheduled_turn`, and `not_found` for a delete of an id that is not the user's.

## Limits

Set by the deployment, never by the agent definition:

| Setting | Default | Meaning |
|---|---|---|
| `SCHEDULE_MAX_PER_USER` | `5` | Schedules one user can hold; enforced inside the store's critical section, so concurrent creates cannot overshoot it |
| `SCHEDULE_MIN_INTERVAL_S` | `3600` | Shortest gap between two runs; a daily or weekly schedule always satisfies it |
| `SCHEDULE_QUIET_START`, `SCHEDULE_QUIET_END` | unset | `HH:MM` bounds, set together, in `SCHEDULE_TIMEZONE`; no run starts inside them |
| `SCHEDULE_TIMEZONE` | `UTC` | Zone of the quiet hours and the default zone of a schedule |
| `SCHEDULE_MAX_PROMPT_CHARS` | `500` | Longest request |
| `SCHEDULE_MISSED_GRACE_S` | `1800` | A run claimed later than this after its window is skipped, not run late |

A schedule whose time of day falls inside quiet hours is refused when it is created. A scheduled turn cannot create schedules.

## Running them

```python
from agents_system.schedules import ScheduledTurnRunner
from agents_system.services.schedules_postgres import PostgresScheduleStore

store = PostgresScheduleStore(scope_provider)  # or InMemoryScheduleStore(provider)
runner = ScheduledTurnRunner(
    store,
    {"sales": agent_runtime},  # runtime id -> AgentRuntime
    sender,  # a ProactiveSender (see Proactive delivery)
    directory,  # the RecipientDirectory the sender uses
    deploy_grants={"sales": {"write:schedule", "send:proactive"}},
    limits=limits,
)
while True:  # in each worker process
    outcome = await runner.run_once()
    if outcome.status == "idle":
        await asyncio.sleep(30)
```

Like the proactive dispatcher, the runner is a loop the deployment runs in its worker processes; the served app does not start one. `run_once` claims one due schedule and runs it:

- **Owner as subject, scheduler as actor.** The turn runs with the schedule's owner as the turn subject (same role, same per-user data scope, memory and budget) and `scheduler:<schedule id>` as the actor, under the runtime's own deploy grants. Audit reads "the scheduler, on behalf of this user".
- **Delivered text only, only to the owner.** The reply is queued through `ProactiveSender` for the owner. The recipient directory authorizes the owner before the turn starts (an unauthorized owner costs no model call; `failed:recipient_not_authorized`, or `failed:recipient_lookup_failed` when the directory itself fails) and again at send time, and `send:proactive` must be granted. A queue failure at send time records `failed:delivery_failed`.
- **Once per window.** The claim stores the next window in the same transaction (`FOR UPDATE SKIP LOCKED` on Postgres), so one window runs at most once across workers and restarts. A run that dies after the claim is not repeated, and a claimed window that fails (a recipient directory or queue outage, a failed turn) is recorded as `failed` and not retried: the worker loop survives and the schedule moves on to its next window.
- **Missed windows are skipped.** A window claimed after `SCHEDULE_MISSED_GRACE_S`, or inside quiet hours, does not run; the schedule moves to its next window and records `skipped:missed_window` or `skipped:quiet_hours`. An interval schedule keeps its cadence: the next window is the first one on its original grid after the claim, so a late claim never shifts an hourly report.
- **Budget.** Usage counts against the owner's [budget](end-user-scope.md#usage-and-budgets-per-end-user). A turn the budget refuses delivers nothing (`skipped:budget_refused`).
- **Still permitted.** The runner checks that the deployment still grants `write:schedule` to the runtime before every run (`skipped:not_permitted`).

`run_once` returns a `RunOutcome` (`idle`, `delivered`, `skipped` or `failed`, with a reason); the last one is also stored on the schedule as `last_status`. "Delivered" means queued: the [dispatcher](proactive.md) sends it with its own guarantees (rate limits, quiet hours, at most once).

## Storage, export and delete

`subject_schedules` (migration `013`) is keyed by (`runtime_id`, `subject_key`, `id`) and fenced by row-level security, like every per-user table. Create, list and delete go through the scope handle; the runner's claim sets a transaction-local worker setting, the only thing besides the subject pair that the policy lets through. The row holds the request in the user's words and no address.

The store is a registered `SubjectDataStore` named `schedules`: the operator export lists a user's schedules and the operator delete removes them, so **a schedule ends when it is deleted or when its owner is deleted.** It has no retention setting. Without Postgres, `InMemoryScheduleStore` over `InMemoryScopeProvider` is the reference backend.

## Evals

The `schedules` capability ships a fixture agent (`schedules-agent`) and three scenarios: `schedule_create` (a weekly request becomes the right schedule), `schedule_cancel` (a schedule is looked up and deleted) and `schedule_limit` (a refused, too-frequent schedule is reported and the allowed one created). See [Live eval](live-eval.md).
