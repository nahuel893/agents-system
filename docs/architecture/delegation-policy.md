# Delegation Policy

## Default: no delegation

An agent runtime does not spawn child agents unless its `policy.md` explicitly permits it (`delegation_policy.allowed: true`) and the platform has injected the appropriate orchestration policy.

Delegation is a capability, not a default assumption. An agent that has not been granted delegation rights cannot initiate it, regardless of what the model decides to reason about.

> **Open decision (2):** Should agents be allowed to autonomously decide to spawn child agents when they determine it would be beneficial, or should all child-agent spawning occur only under explicit system rules that were defined at design time? The current policy is the conservative default (explicit rules only), but the platform's architecture does not technically prevent a future "autonomous delegation" mode if policy allows it. This decision affects safety guarantees, auditability, and the predictability of execution graphs.

---

## When delegation is permitted

The agent's `policy.md` (`delegation_policy` section) governs whether a runtime may delegate. The platform will only inject orchestration policy if `delegation_policy.allowed: true` in the agent's `policy.md`.

Even when allowed, delegation is only appropriate in the following exceptional cases:

| Case | Description |
|---|---|
| **Task decomposition** | A task is too large or too heterogeneous to execute within a single role's scope. The parent decomposes it into sub-tasks and delegates each to an appropriate child role. |
| **Parallel research** | Multiple independent information-gathering sub-tasks can run concurrently. Each child operates on a distinct information domain. |
| **Isolated tool execution** | A tool operation requires permissions or context that should be isolated from the parent's context for privacy or least-privilege reasons. A child is spawned with exactly those permissions, executes the tool, and returns only the result. |
| **Verification / review split** | A first runtime produces an output; a second runtime with a fresh context reviews or verifies it. This prevents the reviewing agent from being anchored to the producer's reasoning. |
| **Protected context boundaries** | Two parts of a task require access to context sets that must never coexist in the same runtime (e.g., data from two tenants). Each child receives only the context appropriate to its sub-task. |

These are exceptional cases, not the default operating mode.

---

## Orchestration policy types

When the platform injects delegation capabilities into a runtime, it injects one or both orchestration policy modules:

### `orchestrator_generic`

Defines universal orchestration behavior applicable to any delegating agent, regardless of domain:

- **Spawn lifecycle** — how to request a child runtime, what to pass as initial context, and how to receive the result
- **Delegation rules** — which role names may be used as child roles, subject to `permitted_child_roles` in the manifest
- **Audit expectations** — every delegation event must be logged: child role, reason for delegation, inputs passed, outputs received
- **Handoff semantics** — how to pass context to a child and how to integrate the child's result back into the parent's execution
- **Safety limits** — maximum delegation depth, maximum concurrent children (when relevant), and what to do when a child fails

### `orchestrator_role`

Defines domain-specific orchestration behavior for a particular parent role:

- **Permitted child roles** — which specific roles this parent is allowed to spawn, beyond the general `permitted_child_roles` list (may further restrict it)
- **Delegatable tools** — which tools from the parent's surface may be passed to or accessed by a child
- **Domain-specific escalation rules** — conditions under which the parent must escalate rather than delegate
- **Role-specific constraints** — any additional domain constraints on how this particular role may orchestrate children

---

## Employee agent as temporary local orchestrator

When an employee agent's `policy.md` permits delegation, the employee agent temporarily acts as a **local orchestrator limited to its own domain**. It does not gain global orchestration rights. It can only spawn children within the scope defined by its own `orchestrator_role` policy.

This is distinct from the Orchestrator Agent, which is a dedicated role with top-level routing and lifecycle control responsibility.

---

## Safety limits

The following limits apply to all delegation scenarios:

| Limit | Rule |
|---|---|
| **Maximum depth** | Set per agent definition in `policy.md` (`delegation_policy.max_depth`). The platform enforces this as a hard limit. A parent at depth N may only spawn children up to depth N+1 if N+1 ≤ max_depth. |
| **Permission inheritance** | A child agent never inherits the parent's full permission set. The child receives only the permissions appropriate to its own role, evaluated independently against the requesting user's RBAC set. |
| **Context leak prevention** | The parent may pass specific, bounded context items to a child. It may not pass its full injected context. What is passed must be declared in the delegation call and logged in the audit trail. |
| **No upward permission escalation** | A child may not request or receive permissions that its own agent definition does not declare, regardless of what the parent holds. |

---

## Escalation vs. delegation

These are distinct mechanisms that must not be confused:

| Concept | What it means | Who executes next |
|---|---|---|
| **Delegation** | The current agent spawns a child to perform a sub-task and waits for the result | A child agent runtime |
| **Escalation** | The current agent terminates its own execution and hands the situation to a higher-authority agent or human | A human operator or a different role |

Delegation keeps control within the agent system. Escalation exits the autonomous execution path.

The escalation rules are declared in the agent's `policy.md` under `escalation_rules`. They are enforced by the harness at execution time, not left to the model's judgment.

---

## Cross-references

- Agent delegation policy: `docs/platform/policy.md`
- Capability injection pipeline: `docs/platform/harness.md`
- Permission constraints on delegation: `docs/architecture/permission-model.md`
