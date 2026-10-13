# Policy

A policy defines how an agent runtime behaves — not what it is allowed to do (that is the permission model), but how it acts within those boundaries.

Policies are injected last in the capability injection sequence, after tools, skills, context, permissions, and memory. They govern the runtime's behavior when using all other injected capabilities.

---

## Policy vs permission model

| Concern | Answered by |
|---|---|
| Can this role call this tool? | `permission-model.md` |
| When should the agent escalate instead of acting? | `policy.md` |
| What happens if a tool call is blocked? | `policy.md` |
| How long should the agent wait before timing out? | `policy.md` |
| Which actions require a human to confirm? | `policy.md` |

---

## Autonomy levels

An agent runtime operates at one of three autonomy levels. The autonomy level is declared in the agent's `policy.md`. It may be narrowed (but not elevated) at runtime by the platform's global policy floor.

| Level | Behavior |
|---|---|
| `full` | The agent acts on all permitted tools without requesting confirmation. Suitable for low-risk, reversible operations. |
| `supervised` | The agent may act autonomously within a defined scope. Actions outside that scope require escalation or human confirmation. |
| `confirm` | The agent proposes actions and waits for explicit human approval before executing any tool call. Suitable for high-risk or irreversible operations. |

The autonomy level is a ceiling, not a floor. A `supervised` agent operating on a sensitive connector may still require confirmation for specific tool calls, as defined in the tool's `sensitive: true` flag and the permission model's revalidation rules.

---

## Untrusted input (ADR-002 C.11)

`untrusted_input: bool` is declared in the agent's `policy.md`. It answers a question distinct from the permission model: not "can this role execute commands on the host" (the `exec:*` permission family), but "can this role's input come from someone who is not a trusted operator" — a customer over a message channel, for example.

| Value | Meaning |
|---|---|
| `true` | The role's input can come from an untrusted, external source (e.g. a customer over WhatsApp). |
| `false` | The role is only ever invoked by a trusted internal actor (an operator, another platform role, a scheduled job). |

### Invariant — mutually exclusive with `exec:*`

A role may never combine `untrusted_input: true` with any `exec:*` permission. This is the platform's defense against the "lethal trifecta" (private data access + untrusted input + an exfiltration channel, together in one agent): once a role holds host execution, the only deterministic defense left is refusing it untrusted input. The loader enforces this as a `DefinitionError` at role-resolution time, on every path `resolve()` can return through — including a role resolved with no deployment override.

| `untrusted_input` | holds `exec:*` | Valid? |
|---|---|---|
| `false` | `false` | ✅ |
| `false` | `true` | ✅ |
| `true` | `false` | ✅ |
| `true` | `true` | ❌ `DefinitionError` |

### Monotonic, once true

Once a role declares `untrusted_input: true`, neither an `extends:` descendant nor a deployment override may set it back to `false` — role-to-role composition is checked exactly like a deployment override, because this flag is a fact about where untrusted input can reach, not a policy choice a role's own author is trusted to loosen. Declaring nothing inherits the parent's (or resolved role's) value in either direction. Both directions may only restrict, never elevate trust back — the same direction every other override invariant in this file enforces.

If nothing in a role's `extends:` chain declares the field at all, it defaults to `false` (untrusted). Every concrete platform role is required, by a dedicated test, to declare it explicitly somewhere in its chain — the implicit default is a floor for roles this document does not cover, not a value any shipped role actually relies on.

Full design rationale: `docs/architecture/adr-002-agent-model-and-capabilities.md`, section C.11.

---

## Escalation rules

An agent must escalate when any of the following conditions are met:

| Condition | Required action |
|---|---|
| A required tool is not in the injected surface | Escalate — do not attempt to proceed without it |
| A tool call is blocked by the interceptor | Escalate — do not silently fail or retry with a different tool |
| Confidence in the correct action is below the threshold defined in the agent's `policy.md` | Escalate — do not guess |
| The user's intent is ambiguous after N clarification attempts | Escalate — do not keep asking |
| A sensitive action would exceed the autonomy level | Escalate — do not downgrade the action to make it fit |
| A child agent delegation fails | Escalate to parent or human depending on depth and policy |

Escalation always produces an audit entry. Silent failures are not permitted.

---

## Human-in-the-loop thresholds

> **Open decision #3:** The exact thresholds for human approval vs autonomous execution are not yet finalized. The categories below are directional.

### Always autonomous (no confirmation required)
- Read operations: catalog lookups, client lookups, session reads
- Message composition that has not yet been sent
- Internal state transitions

### Confirmation required
- Order persistence (writing to the database on behalf of a client)
- Any action that triggers an external system write (ERP, delivery system)
- Escalation to a human operator (Slack notification, handoff)

### Human approval required (agent blocks until response)
- Actions not covered by the agent's current definition
- Actions flagged by the tool interceptor as out-of-scope
- Any action the agent itself classifies as uncertain at `confirm` autonomy level

---

## Tool call enforcement

The platform enforces tool boundaries through two independent layers. Both must be present.

### Layer 1 — Capability Injector (build time)

At injection time, the Capability Injector builds the permitted tool surface from the agent's `manifest.md` and the requesting identity's permission set. Only tools that pass both checks are included.

The injected surface is passed to the LLM via `bind_tools()`. The model can only see and call tools in this list. Tools outside the list do not exist from the model's perspective.

This is the primary enforcement barrier.

### Layer 2 — Tool Call Interceptor (execution time)

Before any tool connector executes, the Tool Call Interceptor validates the invocation against the injected surface for the current runtime.

If the tool is not in the injected surface — regardless of how the call was generated — the interceptor:

1. Blocks the execution
2. Logs a policy violation to the audit trail (tool name, runtime identity, role, timestamp)
3. Triggers escalation according to the agent's `policy.md` escalation rules

The interceptor does not attempt to find an alternative tool, substitute a similar one, or silently ignore the violation. The agent escalates.

This layer exists to catch cases that Layer 1 cannot: model hallucinations of non-existent tools, prompt injection attempts that try to invoke out-of-scope connectors, and bugs in the injection pipeline that cause an incomplete surface.

### Enforcement violation response

| Scenario | Response |
|---|---|
| Tool not in injected surface | Block + audit log + escalate |
| Tool in surface but permission revalidation fails at execution time | Block + audit log + escalate |
| Delegation to a role not permitted by delegation policy | Block + audit log + escalate |
| Sensitive tool called at `full` autonomy without revalidation | Block + audit log + escalate |

In all cases: **no silent failures, no retries with different tools, no workarounds.**

---

## Execution limits

These are platform-level defaults. An agent's `policy.md` may define stricter values but not looser ones. Only the deployment can raise them (below).

| Limit | Default | Notes |
|---|---|---|
| Tool call timeout | 10s | Per individual tool call |
| Total execution timeout | 60s | From trigger receipt to final output |
| Max tool calls per execution | 20 | Prevents runaway loops |
| Max tool errors per turn | 3 | A tool that raised or timed out; reaching it ends the turn with a controlled reply (see "Tool errors") |
| Max delegation depth | 2 | Parent → child → grandchild; no deeper |
| Max clarification attempts | 3 | Before forcing escalation on ambiguous input |

### Raising limits for a deployment

An agent definition alone can never raise a limit. The deployment, which pays for and operates the service, can: it sets `total_execution_timeout_s`, `tool_call_timeout_s`, `max_tool_calls` and `max_tool_errors` per runtime id, and each value replaces the platform default as that runtime's ceiling. The agent's own `execution_limits` may still tighten it, never exceed it.

| Where | How |
|---|---|
| Served app | `create_app(execution_limits={"reports": {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}})`, or the `EXECUTION_LIMITS` variable, keyed by runtime id like `DEPLOY_GRANTS` |
| Library | `build_runtime(agent, registry, grants, execution_limits={...})`, or `AgentRuntime(equipped, execution_limits={...})`; given to both, the tighter value of each key wins |

- **A key the deployment leaves out** keeps the platform default. A value below the default tightens the runtime instead. A tool-call timeout that neither the deployment nor the agent sets is capped at the turn's, so a deployment may lower only the turn, even below 10 s.
- **The agent tightens.** Under a 240 s deployment, an agent declaring `total_execution_timeout_s: 90` runs 90-second turns, and one declaring 300 fails to load. A platform role with a value of its own (`developer-agent`'s 120 s turn, `operator-agent`'s 10 s tool calls) keeps it when the deployment's is higher and is capped when it is lower. Under a deployment that lowers the turn to 30 s, an agent declaring 50 s still loads and runs 30-second turns: capped, not refused.
- **Declaring above the default needs the limits at load.** An agent may declare above the platform default only when the limits reach the loader: through `build_runtime(execution_limits=...)` or the served deployment (`create_app`, `EXECUTION_LIMITS`). `AgentRuntime(execution_limits=...)` alone caps the runtime, but the definition was already loaded against the platform defaults.
- **Checked combined.** Each input is checked alone, and the limits the runtime will run with are checked again once the deployment's and the agent's are combined, at construction and at boot for every runtime with deployment limits. An agent's 70 s turn under a deployment's 100 s tool calls fails, naming the runtime.
- **Bounded by the outbox lease.** A turn and its reply send must finish inside the 600 s webhook outbox lease, or another worker can reclaim the message and answer twice. `LEASE_MARGIN_S` (in `agents_system.execution_limits`) reserves 60 s of it for the reply send and the checkpointer reads and writes, so a turn must be shorter than 540 s (`max_turn_timeout_s()`). A turn that does not fit, or a `tool_call_timeout_s` above the turn's, fails at boot, and at construction in library use, with an error naming the runtime and the bound. So do an unknown key, a value that is not a positive number, and a limit for a runtime the boot does not build.
- **The margin holds the checkpointer.** Of the 60 s, `REPLY_SEND_ALLOWANCE_S` keeps 20 s for the reply send, and the rest holds the thread read and save, each bounded by `CHECKPOINTER_TIMEOUT_S`. So `CHECKPOINTER_TIMEOUT_S` may be at most 20 s (`max_checkpointer_timeout_s()`); above it, a boot that binds a runtime to a channel with a checkpointer fails. Past the margin, a turn near the bound would outlast the lease and run again on another worker, tools included.
- **Logged.** Each runtime logs `runtime.execution_limits` with the limits it booted with and the keys the deployment set; `AgentRuntime.execution_limits` returns them.

### The timeout reply

A turn that runs out of time answers "This is taking longer than expected. Please try again." A deployment serving users in another language sets its own text per runtime: `create_app(timeout_reply={"support": "..."})`, the `TURN_TIMEOUT_REPLY` variable (`{"runtime id": "text"}`), or `AgentRuntime(timeout_reply=...)`. It is plain text of at most 1000 characters, sent exactly as given: never templated with user or tool data.

### Tool errors

*Coming in the next release.* A tool that raises does not abort the turn. The runtime hands the model an error result (a `ToolMessage` with `status="error"`, the same shape as a tool timeout) and the model gets another step, so it can shorten an argument or change approach. The text is one line: the tool name, the exception class and its message cut to 300 characters, for example `Tool 'run_code' failed: ValueError: codigo must be a non-empty string`. It never carries a traceback. It is truncated and flattened, not scrubbed: the platform has no secret-redaction helper for it, so a tool must not put a secret in an exception message. The call is still recorded as an `"error"` outcome in `agent_tool_calls_total`, and the runtime logs `runtime.tool_error` with the tool and the exception class.

**The cap.** `max_tool_errors` (default 3) counts every error result in a turn, whether the tool raised or timed out. When the count reaches it, the turn ends with a controlled reply instead of letting the model retry again, and `agent_limit_trips_total{limit="max_tool_errors"}` is incremented. It is an execution limit like the others: the agent may tighten it, the deployment may set it (`create_app(execution_limits={"support": {"max_tool_errors": 5}})`, `EXECUTION_LIMITS`), and it must be a positive whole number. The default reply is "I could not complete this because a tool kept failing. Please try again or rephrase your request." A deployment sets its own text per runtime: `create_app(tool_error_reply={"support": "..."})`, the `TOOL_ERROR_REPLY` variable (`{"runtime id": "text"}`), or `AgentRuntime(tool_error_reply=...)`. It is plain text of at most 1000 characters, sent exactly as given.

**What still aborts the turn.** Only an `Exception` becomes an error result. The set below carries a security or permission decision, so it propagates and aborts the turn: handing it to the model as a retryable error would invite it to try around the decision. The set is defined in one place, `aborting_exceptions()` in `agent/tool_errors.py`.

| Exception | Why it aborts |
|---|---|
| `AgentPermissionError` (and subclasses) | The permission model failed: a decision about what may run |
| `ProactivePermissionError` | The caller lacks `send:proactive` within the deploy grants |
| `RecipientNotAuthorizedError` | The deployment's directory does not authorize the recipient |
| `QueryRejectedError` | The SQL guard refused a query that the connector did not answer itself |
| `UnsafeQueryRoleError` | The database role is unsafe for model-authored SQL |
| `UnfencedRoleError` | The database role bypasses row-level security |
| `BaseException` (`asyncio.CancelledError`, `KeyboardInterrupt`) | Never caught: the turn's own total timeout works by cancelling it |

Exceptions from an optional extra that is not installed cannot be raised and are skipped. The interceptor's own denials (`PolicyViolation`: tool not in the surface, permission revoked, revalidation required, memory policy, missing subject) were never raised out of the turn: they were already returned to the model as a "blocked" error result, still are, and do not count toward `max_tool_errors`.

---

## Audit policy

`audit_policy` is read by the audit subsystem at emit time. Every key is
optional; the defaults are the safe ones.

| Key | Type | Default | Effect |
|---|---|---|---|
| `retention_days` | `int \| null` | platform default | How long records are kept before the partition is dropped |
| `capture_tool_input` | `bool` | `false` | When `true`, free-text values (`message`, `body`, `text`) are stored instead of redacted |
| `redact_keys` | `list[str]` | `[]` | Additional top-level payload keys to always redact |

`capture_tool_input` is a narrow opt-in, not a global one. Phone numbers and
email addresses are stripped even when it is enabled — turning on free-text
capture is not turning on storage of customer identifiers.

Implementation and the full redaction table: `docs/platform/audit.md`.

---

## Model configuration (D-037, ADR-004, issue #13)

An agent's `policy.md` (a predefined role's own, a deployment override's, or
the `Agent`/`Agent.from_folder` Python API's `model=` parameter) may declare
which already-configured provider + model it runs on:

```yaml
model:
  provider: anthropic          # ollama | groq | anthropic | openai_compatible
  model: claude-3-5-sonnet-latest
  temperature: 0.2             # optional, 0-2
  max_tokens: 1024              # optional, positive integer
```

| Key | Required | Notes |
|---|---|---|
| `provider` | yes | One of the four values `ADAPTER_PROVIDER` accepts. |
| `model` | yes | The provider's own model id (non-empty string). |
| `temperature` | no | A finite number between 0 and 2. |
| `max_tokens` | no | A positive integer, at most 1,000,000 (a sanity ceiling that catches a misplaced-decimal or unit-mixup typo, not a real provider limit). |

No other key is accepted. This is closed deliberately: `provider`+`model`
select WHICH already-configured provider/model to use, never a credential
-- an API key, token or secret can never live in a `model:` block, in this
file or in any deployment override. Every actual credential
(`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, ...) is
read exclusively from `Settings`/the process environment, the same as
every provider the platform already supported. A `model:` block naming an
unknown key, an unsupported `provider`, a missing/empty `model`, or a
non-numeric `temperature`/`max_tokens` fails resolution with a
`DefinitionError` naming the field and the reason.

### Default: the global provider stays the default

Declaring nothing (the overwhelming majority of roles today) is unchanged:
the runtime uses the single, global `ADAPTER_PROVIDER`-driven model every
existing deployment already runs on. Opting a role or deployment into its
own `model:` never affects any other runtime.

### Inheritance and override

- **Role-to-role `extends:`** folds `model` per key, the same shape as
  `execution_limits`: a child may declare only `temperature: 0.7` and still
  inherit `provider`/`model` from its parent. A child's `model:` must still
  be a mapping, `inherit`, or omitted; any other value (a bare scalar, a
  malformed string) fails resolution immediately, even when the parent's
  own `model:` is perfectly valid — it is never silently discarded in
  favor of the parent's.
- **A deployment override** replaces the role's `model:` wholesale, not per
  key -- picking a provider+model for a client is a deliberate, complete
  choice, not a partial tweak. A deployment that declares no `model:` at all
  keeps the role's own.
- **No ceiling.** Unlike `autonomy`/`execution_limits`/`audit_policy`,
  model/provider selection is not a capability or safety escalation -- it
  never widens what a role can do -- so an importer-authored `Agent` or a
  deployment override may name ANY supported provider+model, never
  restricted to "stricter than the parent."

### Validated at boot, not at first request

Every registered runtime's `model:` (if any) is resolved and its chat model
is actually constructed during application startup (`main.py`'s
`lifespan`), inside the same loop that already builds every other runtime
-- a missing `OPENAI_COMPATIBLE_BASE_URL`, a missing provider credential
(`ANTHROPIC_API_KEY`, `GROQ_API_KEY`, ...), an unsupported provider, or any
other malformed declaration fails boot loudly, the same posture every other
boot-time configuration check in this file already has. Nothing here is
deferred to the first inbound message. When the failing `model:` belongs to
one specific agent's own declaration (rather than the global
`ADAPTER_PROVIDER` default every runtime without one falls back to), the
boot error names that agent's role too.

### In-process use (issue #186)

The declaration is honored without the server too: `AgentRuntime(equipped)`
with no `model=` builds the declared model through the same implementation the
served app uses (`agents_system.agent.models`), reading credentials from the
environment only. A caller-passed model that differs from the declared one
wins and is reported with a `runtime.model_conflict` warning. See
`docs/platform/library-usage.md`, "Choosing the model".

### Cost tracking is unaffected

`MODEL_PRICES` (`Settings.model_prices`, see `docs/platform/live-eval.md`)
stays keyed by the actual constructed model's own provider model id, which
is unchanged by this feature: a runtime built from its own `model:`
declaration is priced exactly the same way a runtime built from the global
default already was.

---

## Failure modes

| Failure | Behavior |
|---|---|
| Tool connector unreachable | Retry once after 2s; if still failing, escalate |
| LLM provider unreachable | Fail fast, return error to caller, audit log |
| Injection pipeline error (missing tool) | Block execution entirely, alert platform operator |
| Audit persistence failure | **Does not block.** The event is dropped, `audit.event_dropped` is logged, and `dropped_count` increments |

### Open decision: auditability vs. availability

This specification originally read *"Block execution — no execution without
auditability"*, and the implemented `AuditSink` does the opposite: it is
fire-and-forget by construction, so an audit write failure never reaches the
request path.

Both positions are defensible and they are genuinely incompatible:

- **Block on failure** is the right posture if the audit trail is a compliance
  artifact — an unauditable action must not happen.
- **Never block** is the right posture if availability is the priority — the
  audit subsystem must not become a latency or failure dependency of customer
  traffic.

The code currently implements the second. This row was corrected to match the
code rather than the code changed to match the row, because reversing it is a
platform-level decision with real consequences for the WhatsApp path, not a bug
fix. It is recorded here so the choice is explicit rather than accidental.

---

## Cross-references

- Permission model and RBAC: `docs/architecture/permission-model.md`
- Tool definitions and `sensitive` flag: `docs/platform/tool.md`
- Delegation rules: `docs/architecture/delegation-policy.md`
- Enforcement implementation in the harness: `docs/platform/harness.md`
