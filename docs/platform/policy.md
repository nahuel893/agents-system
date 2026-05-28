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

These are platform-level defaults. An agent's `policy.md` may define stricter values but not looser ones.

| Limit | Default | Notes |
|---|---|---|
| Tool call timeout | 10s | Per individual tool call |
| Total execution timeout | 60s | From trigger receipt to final output |
| Max tool calls per execution | 20 | Prevents runaway loops |
| Max delegation depth | 2 | Parent → child → grandchild; no deeper |
| Max clarification attempts | 3 | Before forcing escalation on ambiguous input |

---

## Failure modes

| Failure | Behavior |
|---|---|
| Tool connector unreachable | Retry once after 2s; if still failing, escalate |
| LLM provider unreachable | Fail fast, return error to caller, audit log |
| Injection pipeline error (missing tool) | Block execution entirely, alert platform operator |
| Audit persistence failure | Block execution — no execution without auditability |

---

## Cross-references

- Permission model and RBAC: `docs/architecture/permission-model.md`
- Tool definitions and `sensitive` flag: `docs/platform/tool.md`
- Delegation rules: `docs/architecture/delegation-policy.md`
- Enforcement implementation in the harness: `docs/platform/harness.md`
