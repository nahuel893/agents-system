# Proactive outbound delivery

A channel adapter normally only replies to a verified inbound turn. Proactive delivery is the other direction: a message to an authorized end user with no inbound turn, such as a scheduled report, a reminder, a welcome message or an alert when a long job ends. It runs inside the platform's identity, grants, audit and rate limits instead of an ad-hoc script against the provider API. Design: [ADR-011](../architecture/adr-011-end-user-scope.md) part 5.

It is **not a model-facing tool**. The first callers are scheduled turns (#175) and operator commands; the model never chooses a recipient or an address.

## The pieces

```python
from agents_system.proactive import (
    ProactiveDispatcher,
    ProactivePolicy,
    ProactiveSender,
    StaticRecipientDirectory,
)
from agents_system.services.proactive_postgres import PostgresProactiveQueue
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject import TurnActor, TurnSubject

queue = PostgresProactiveQueue(PostgresScopeProvider(engine))
directory = MyDirectory()  # your RecipientDirectory, see below

sender = ProactiveSender(
    queue, directory, deploy_grants={"support": {"send:proactive"}}
)
await sender.submit(
    "support",  # runtime id
    TurnSubject.for_participant(42),
    "Your report is ready.",
    actor=TurnActor(kind="scheduler", id="nightly-report"),
    permissions=current_permissions,  # the caller's grants, checked now
)

dispatcher = ProactiveDispatcher(
    queue,
    directory,
    {"whatsapp": whatsapp_channel},  # channel name to ProactiveChannel
    deploy_grants={"support": {"send:proactive"}},  # the same ceiling
    policy=ProactivePolicy(per_recipient=(3, 86400), per_runtime=(500, 3600)),
)
while True:  # in each worker process
    outcome = await dispatcher.run_once(worker_id)
```

- **The recipient is a subject, never an address.** `RecipientDirectory.authorize(runtime_id, subject)` is a port the deployment implements (an allowlist, a database, a CRM). It returns the subject with its `channel` and `address`, or `None` to refuse. `StaticRecipientDirectory` is the in-process reference. The directory is asked twice: at `submit` (an unauthorized recipient raises `RecipientNotAuthorizedError` and nothing is queued) and again at dispatch, so a recipient revoked in between is refused before any provider call. The queue stores no address; it is resolved at send time, so a reassigned number is never messaged.
- **`send:proactive`.** A new T2 permission, a sibling of `send:message` and `send:escalation`: an existing `send:message` grant never covers it. `submit` revalidates it at call time against the deploy ceiling **of the runtime id it is given** and the caller's current permissions, and raises `ProactivePermissionError` otherwise. `deploy_grants` is a mapping from runtime id to permission names (the `DEPLOY_GRANTS` shape); a runtime with no entry has no grant. The dispatcher revalidates at send time: it rechecks the ceiling for the message's runtime and, when you pass `current_permissions=async (runtime_id, actor_label) -> permission names`, the caller's current grants. A message whose grant was revoked or whose ceiling was narrowed while it waited (durable queue, quiet hours, a 429 delay) ends `failed` with reason `permission_revoked` and never reaches the provider. Without `current_permissions` the queue holds no record of the caller's grants, so only the ceiling is rechecked.
- **Channels.** A channel that can send unprompted implements `ProactiveChannel.send_proactive(to, body)`. The WhatsApp channel does: a `429` becomes `ProviderRateLimited(retry_after_s)` with the `Retry-After` delay (delay-seconds or an HTTP-date, bounded to 24 hours; a missing, invalid or past value falls back to 60 seconds), any other `4xx` becomes `ProviderRejected`, and a `5xx` or a transport error is left as it is. Outside Meta's 24-hour customer-service window Meta accepts only approved templates; the adapter sends plain text, so use it for users who wrote recently, or implement a template send in your own adapter.
- **Durable, once.** `PostgresProactiveQueue` keeps messages in `proactive_outbox` (migration `012`), a per-user table keyed by (`runtime_id`, `subject_key`, `id`) with row-level security through the scope handle. A worker claims a message with `FOR UPDATE SKIP LOCKED` and a lease, the same discipline as the webhook outbox, so a message goes out once across workers and restarts. The dispatcher's claim runs with the table's worker setting on, because it serves whichever subject is due; enqueue, export and delete are fenced per subject. The table is a registered `SubjectDataStore` (`proactive`): the operator export and delete cover it, and `SUBJECT_DATA_RETENTION='{"proactive": 2592000}'` purges finished rows. Constructing `PostgresProactiveQueue(provider)` binds that store to the same database, and the served app registers and binds it at boot when `create_app(scope_provider_factory=...)` is configured, so export, delete and retention reach queued bodies in either setup.
- **Library mode.** `InMemoryProactiveQueue` is the same contract in one process, without persistence.

## At most once

The reply path never resends an ambiguous send, and neither does this one. After the message is claimed:

| What happened | Result |
|---|---|
| The provider accepted it | `sent` |
| `429` with a delay | Back to `queued`, due after `Retry-After`. `run_once` returns `rate_limited` with `retry_after_s`; nothing sleeps or loops, and the next claim waits for the due time |
| A definitive refusal (`4xx`) | `failed`, not retried |
| A timeout, a `5xx`, any error whose outcome is unknown | `ambiguous`, not retried |
| The worker died after claiming (the lease lapses) | `ambiguous`, not retried; the next `run_once` of any worker audits it once (reason `lease_expired`) |

An `ambiguous` or `failed` message stays in the table (with `last_error`) for the operator to see; nothing resends it.

## Guardrails

`ProactivePolicy` defers a message instead of dropping it:

- `per_recipient=(count, window_s)` and `per_runtime=(count, window_s)`: a message over the limit is requeued and rechecked after `recheck_s` (default 60).
- `quiet_hours=(start, end)` with `tz`: inside the window (it may wrap midnight) the message waits until it ends.

## Audit

Every step records a `proactive_delivery` event (`queued`, `sent`, `failed`, `ambiguous`, `rate_limited`, `deferred`) with the **actor** that triggered it (`scheduler:<id>`, `operator:<id>`, `library:<id>`) in `actor` and the subject key in `subject`. The payload holds the runtime id, message id, channel name and a reason code; never the address or the text. A lease expiry is audited by the worker that first sees it, once per message; if that worker dies between the state change and the audit write, the event is lost (audit is best effort) but the row keeps `ambiguous` and `last_error`, which the operator export shows.
