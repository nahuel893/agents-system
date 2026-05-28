# Harness

The harness is the infrastructural layer that wraps around a language model and turns it into an autonomous, functional agent.

The model is the brain — it processes input and produces output. The harness is the operating system: it provides the hands, eyes, and memory the model needs to interact with the real world.

| Layer | What the harness provides |
|---|---|
| **Hands** | Tools — connectors and executors that let the agent take actions: send a message, write a record, query a database |
| **Eyes** | Context pipeline — structured perception of the world: the triggering event, the user identity, the session state, the organizational knowledge the agent is allowed to see |
| **Memory** | Persistent and working memory handles — short-term session state, team-scoped working memory, long-term business memory |

Without the harness, a model can only read and write text. With it, the model becomes an agent that perceives its environment, acts on it, and remembers across interactions.

This document describes the harness's full execution pipeline, capability injection model, lifecycle, and isolation constraints.

---

## Full pipeline

```
Trigger
  → System Router
    → Agent Factory
      → Capability Injector
        → Agent Runtime
          → Tool Execution / Delegation
            → Audit / Memory
```

Every agent execution follows this pipeline. There are no shortcuts.

---

## Stage descriptions

### 1. Trigger

A trigger is any event that initiates an agent execution. The platform accepts the following trigger types:

| Type | Examples |
|---|---|
| User action | WhatsApp message, API call, web form submission |
| Message | Inbound message from an integration connector |
| Schedule | Cron-triggered batch processing, timed reminders |
| External event | Webhook from a third-party service, queue message |
| Tool callback | A tool's async result arriving from an external system |

Triggers carry: the raw event payload, a source identifier (which integration it came from), and a requesting identity (user/employee, or system for scheduled triggers).

---

### 2. System Router

The System Router receives the trigger and performs three decisions:

1. **Domain identification** — which domain and subsystem does this trigger belong to?
2. **Role selection** — which agent definition should handle this trigger?
3. **Runtime reuse** — is there a valid warm-cached runtime for this role and identity, or must a new one be instantiated?

The System Router does not execute any agent logic. It produces a routing decision: a role name, a requesting identity, and a cache directive (`use_cache: true/false`).

---

### 3. Agent Factory

The Agent Factory receives the routing decision and builds the base runtime:

- Resolves the model and provider (e.g., Claude Sonnet 4 for conversation, Claude Haiku 4.5 for classification)
- Applies the baseline execution policy (timeout, retry policy, temperature)
- Reads the agent definition folder for the selected role (`role.md`, `manifest.md`, `policy.md`)
- Produces an unequipped runtime — a runtime with identity and policy but no injected capabilities yet

The factory does not inject capabilities. That is the Capability Injector's responsibility.

---

### 4. Capability Injector

The Capability Injector receives the unequipped runtime and the requesting identity, and injects capabilities in a fixed order. The order is not arbitrary — each injection step may depend on what was injected before it.

**Injection order:**

1. **Tools** — inject connectors and executable capabilities declared in the agent's `manifest.md`. Permission check happens here. Tools are injected first because skills may declare `context_requirements.requires_tool_output`, which requires knowing which tools are available.

2. **Skills** — inject behavioral packs and prompt modules declared in the agent's `manifest.md`. Skills that require tool outputs can verify the dependency was resolved in step 1.

3. **Context** — inject task context, session context, user identity, and organizational context according to the agent's `manifest.md`. Context is injected after skills because skill prompt modules become part of the context surface.

4. **Permissions** — finalize the permission set bound to this runtime. While initial permission evaluation happened during tool injection, the full permission surface (including delegation rights and context access) is resolved here.

5. **Memory** — attach memory handles (local, team, or org-wide) according to the agent's `policy.md` (`memory_policy`). Memory is injected after permissions because read/write scope is permission-dependent.

6. **Policies** — inject execution policies: autonomy level, escalation rules, delegation policy (including orchestration policy modules `orchestrator_generic` and/or `orchestrator_role` if delegation is permitted) sourced from the agent's `policy.md`. Policies are last because they govern the runtime's behavior when using all other injected capabilities.

At the end of injection, the runtime is fully equipped and ready for execution.

---

### 5. Agent Runtime

The Agent Runtime executes the assigned role using its injected capabilities. It:

- Processes the triggering event
- Invokes tools when needed
- Applies injected skills to reason about the task
- Observes escalation rules from the injected policy
- Delegates to child agents if delegation policy permits (see `docs/architecture/delegation-policy.md`)
- Produces outputs (messages, written records, delegated tasks, escalations)

The runtime operates within a strict boundary: it can only use what was injected. It cannot acquire new capabilities during execution.

---

### 6. Tool Execution / Delegation

Tool calls and delegation events are distinct execution sub-steps within a running agent runtime.

#### Enforcement layers

Tool call enforcement is two-layered. Both layers must be active at all times.

**Layer 1 — Capability Injector (build time):** At injection time, only permitted tools are included in the runtime's surface and passed to the model via `bind_tools()`. The model cannot see or call tools outside this list.

**Layer 2 — Tool Call Interceptor (execution time):** Before any connector executes, the interceptor validates the call against the injected surface. If the tool is not in the surface — regardless of how the call was generated — the interceptor blocks execution, writes a policy violation to the audit trail, and triggers escalation. No silent failures. No alternative tool substitution.

This second layer catches what Layer 1 cannot: model hallucinations of out-of-scope tools, prompt injection attempts, and incomplete injection bugs.

For the full enforcement policy including violation responses and execution limits, see `docs/platform/policy.md`.

**Tool execution (permitted call):**
- The runtime selects a tool from its injected surface
- The interceptor validates the call (Layer 2)
- For sensitive tools, permissions are revalidated against current state before the call proceeds
- The tool connector executes the external operation
- The result is returned to the runtime's context

**Delegation:**
- The runtime invokes the delegation policy to create a child runtime
- The child runtime follows the same full pipeline: Factory → Injector → Runtime → Audit
- The child receives only the capabilities it is entitled to — it does not inherit the parent's full capability set
- Results from the child are returned to the parent runtime's context

---

### 7. Audit / Memory

After execution (whether successful, escalated, or failed), the harness persists:

- The audit trail: who triggered, which role, which tools were injected, which tool calls were made, which actions were taken, and their outcomes
- Memory writes: conversation logs, working memory updates, and persistent business memory — according to the runtime's `memory_policy`

Audit trail entries are immutable. Memory writes may be scoped to session, team, or org depending on the manifest's `memory_policy` and the runtime's permission set.

---

## Lifecycle stages

| Stage | Description |
|---|---|
| **Instantiate** | Factory creates the base runtime with identity and baseline policy |
| **Inject** | Capability Injector equips the runtime with tools, skills, context, permissions, memory, and policies |
| **Execute** | Runtime processes the trigger, invokes tools, and produces outputs |
| **Teardown / Cache** | Runtime is destroyed or returned to the warm cache; artifacts are persisted |

---

## Warm cache policy

The platform may maintain a warm cache of pre-instantiated, pre-injected runtimes for high-frequency roles where cold-start latency is unacceptable.

**Conditions for caching a runtime:**
- The role is high-frequency (triggered many times per minute)
- Cold-start latency is measurably impacting user experience
- The injected context is safe to reuse across triggers (no user-private data in the capability surface)

**What must never be cached or reused across users:**
- The requesting user's identity and permission set
- Any context loaded on behalf of a specific user (session context, private wiki access, employee-scoped memory)
- Any tool result that contains user-private data

A warm cache entry is valid only for the same role and the same identity. A cached runtime for user A must never serve user B.

**Cache invalidation triggers:**
- Permission change for the user associated with the cached runtime
- Agent definition folder update
- Explicit cache invalidation signal from the System Router

---

## Context isolation

Each runtime sees exactly what was injected for it. It does not see:

- Other users' session contexts
- Capabilities injected into sibling or parent runtimes
- Org-wide context unless the agent's `manifest.md` declares `context.org_context: true` and the user's permission set allows it
- Memory outside the scope defined by the agent's `policy.md` (`memory_policy`)

When a parent runtime delegates to a child, the child receives a fresh injection scoped to the child's role and the delegating user's identity. The child does not inherit the parent's full context — only what the delegation policy explicitly passes.

---

## Audit trail

The following must be reconstructible from the audit trail for every execution:

| Item | Description |
|---|---|
| Trigger identity | Who or what initiated the execution |
| Role | Which agent definition was active |
| Tools injected | Full list of tools attached at injection time |
| Tool calls made | Each tool invocation: tool name, inputs (sanitized of secrets), output shape, timestamp |
| Delegations | If any child agents were spawned: child role, delegation reason, outcome |
| Escalations | If any escalation was triggered: escalation target, triggering condition, outcome |
| Outputs | What the runtime produced: messages sent, records written, delegations initiated |
| Duration | Wall-clock time from trigger to teardown |

Audit records are retained according to the agent's `policy.md` (`audit_policy.retention_days`), or the platform default if `null`.

---

## Cross-references

- Agent definition schema: `docs/platform/role.md`
- Tool definitions: `docs/platform/tool.md`
- Skill definitions: `docs/platform/skill.md`
- Delegation and child-agent rules: `docs/architecture/delegation-policy.md`
- Permission model and RBAC: `docs/architecture/permission-model.md`
