# Agent Platform Architecture

This document defines the formal architecture of agents-system, a development kit for building AI agents. The smallest path into the kit is the [Quickstart](../platform/library-usage.md#quickstart): an agent with one tool of your own in about ten lines.

## Executive summary

The kit is split into three parts:

1. **Core kit** — the harness: declarative and Python agent definitions, orchestration, lifecycle, permissions, and capability injection. This is what you build agents with.
2. **Optional pieces** — the OpenAI-compatible **server**, the **channels** (WhatsApp is the first one), the **predefined roles** (reusable agent archetypes) and the **reference backends** (Postgres, Redis, embeddings). They are optional at runtime, not at install time: you only turn on the ones you need, but installing the kit still brings the dependencies of all of them (fastapi, asyncpg, redis and torch) until #188, a light core install, changes the packaging.
3. **Your application** — the tools, connectors, business rules and per-client overrides that turn the kit into a specific agent. They live in your own repository, not in this one.

This separation is intentional. The kit is the reusable product. Any one agent built with it, a sales assistant included, is an application of the kit, never the kit itself.

---

## Scope boundary

### Core kit

The core kit owns the generic multi-agent infrastructure:

- declarative role definitions in Markdown
- agent factory / provider runtime
- dynamic injection of tools, skills, context, and policies
- orchestration and delegation rules
- spawn lifecycle for child agents
- RBAC-aware permission enforcement
- memory, audit trail, and execution logging
- optional warm cache for low-latency agent creation

### Optional pieces

The kit ships these so you do not have to build them, and nothing in the core depends on them at runtime. Installing the kit still brings their dependencies:

- the OpenAI-compatible server and its channels
- the predefined roles under `platform/roles/`
- reference backends for persistence, checkpointing and embeddings

### Your application

An application built on the kit owns everything specific to its domain:

- its workflows and conversational logic
- integrations with its own systems, registered as tools
- its rules, vocabulary, approvals, and business constraints
- per-client narrowing of a predefined role

### Not enabled by default

The following capabilities are considered kit capabilities or future modules; an application adopts them explicitly:

- personal employee agents
- meeting summary agents
- expanded data agents
- local workstation runtimes
- hierarchical child-agent delegation flows

---

## Design principles

1. **Declarative first**
   - Agent roles are defined as folders under `platform/roles/`, each containing `role.md`, `manifest.md`, and `policy.md`.
   - Runtime behavior is assembled from those agent definitions, not hardcoded per case.

2. **Instantiate on demand**
   - Agents are created and destroyed under demand, like runtime objects.
   - No always-on swarm by default.

3. **Dependency injection over hidden coupling**
   - Tools, skills, context, permissions, and policies are injected explicitly.

4. **Least privilege**
   - Every agent receives only the minimum capabilities required for the current role and task.

5. **Auditability**
   - Every execution path should be reconstructible: who triggered it, which role was used, what tools were injected, and what actions were taken.

6. **Composable delegation**
   - Agents may delegate only when policy allows it.
   - Delegation is a capability, not a default assumption.

7. **Performance-aware runtime**
   - Agent cold start should be cheap.
   - Warm caches may be used where latency matters.

---

## Architecture overview

```text
Trigger -> System Router -> Agent Factory -> Capability Injector -> Agent Runtime -> Tool Execution / Delegation -> Audit / Memory
```

### Main stages

1. **Trigger**
   - user action
   - message
   - schedule
   - external event
   - tool callback

2. **System Router / Orchestrator**
   - identifies the domain
   - selects role or subsystem
   - decides whether to instantiate a new runtime or reuse a warm one

3. **Agent Factory / Provider**
   - builds the base runtime
   - resolves model/provider selection
   - applies baseline execution policy

4. **Capability Injector**
   - injects tools
   - injects skills
   - injects context
   - injects permissions
   - injects memory handles
   - injects orchestration policy when needed

5. **Agent Runtime**
   - executes the assigned role
   - uses tools
   - collaborates or escalates when policy permits

6. **Teardown / Cache / Persistence**
   - runtime is destroyed, recycled, or returned to a warm cache
   - execution artifacts and memory are persisted according to policy

---

## Formal runtime model

The platform distinguishes clearly between three concepts:

| Concept | Meaning |
|---|---|
| **AgentDefinition** | Folder-based declarative definition of a role (`role.md` + `manifest.md` + `policy.md`) |
| **AgentRuntime** | Live in-memory execution instance |
| **Subsystem** | Coordinated set of roles and policies within a domain |

This distinction prevents mixing role definition, runtime identity, and orchestration boundaries.

---

## Agent model

### Central agents

The initial central topology includes:

- **Orchestrator Agent**
- **Sales Agent**
- **Data Agent**
- **Summary Agent**

These agents are role-driven and instantiated dynamically through the same platform runtime.

### Employee agent

By default, each employee has:

- **one main employee agent**
- instantiated on demand
- scoped to the employee identity and permissions
- not modeled as a permanent swarm

This agent is the employee-facing runtime for personal assistance.

### Optional local runtime

The employee agent may run as a local runtime on the employee workstation using:

- **Hermes Agent**
- **OpenClaw**
- **PicoClaw**

This is optional platform topology, not a universal requirement.

---

## Delegation and child agents

### Default behavior

An employee agent does **not** spawn multiple children by default.

### Exceptional behavior

The employee agent **may** create child agents when required by policy, for example:

- task decomposition
- parallel research
- isolated tool execution
- verification / review split
- protected context boundaries

### Temporary local orchestration

When an employee agent delegates, it temporarily acts as a **local orchestrator limited to its own domain**.

For that mode, the system may inject:

- `orchestrator_generic`
- `orchestrator_role`

#### `orchestrator_generic`

Defines universal orchestration behavior:

- spawn lifecycle
- delegation rules
- audit expectations
- handoff semantics
- safety limits

#### `orchestrator_role`

Defines domain-specific orchestration behavior:

- what kinds of children may be created
- which tools can be delegated
- domain-specific escalation rules
- constraints for that employee role

---

## Agent definitions (folder-based)

Each role is defined as a folder under `platform/roles/` with three Markdown files: `role.md`, `manifest.md`, and `policy.md`.

At minimum, the agent definition should define:

**`role.md`:**
- role name
- purpose
- scope

**`manifest.md`:**
- allowed tools
- allowed skills
- required context
- permission requirements

**`policy.md`:**
- escalation rules
- delegation policy
- memory policy
- audit policy

### Agent definition principle

The agent definition defines **what the role is allowed to be and do**.
The runtime decides **how and when it is instantiated**.

---

## Capability injection model

When a runtime is created, the system injects:

- **Tools** — connectors and executable capabilities
- **Skills** — specialized behavior packs or prompt modules
- **Context** — task, user, org, and session context
- **Permissions** — RBAC and execution boundaries
- **Memory** — local, shared, or persistent handles
- **Execution policies** — autonomy, escalation, delegation, caching

This injection is role-aware and must be explicit.

---

## Lifecycle

### Standard lifecycle

1. trigger received
2. role selected
3. runtime instantiated
4. capabilities injected
5. execution performed
6. outputs persisted
7. runtime destroyed or cached

### Cache policy

The platform may maintain a warm cache of reusable runtimes when:

- cold start latency matters
- the role is high-frequency
- the injected context is safe to reuse

Warm cache must never leak stale permissions or stale private context across users.

---

## RBAC and permission model

Employees are modeled as **RBAC users**.

This implies:

- agent identity is bound to employee identity when acting on behalf of an employee
- tool access must be filtered by role and permission set
- context loading must obey privacy boundaries
- delegation must not bypass RBAC controls

Permissions must be evaluated at injection time and, if necessary, revalidated at execution time for sensitive actions.

---

## Memory and context

The platform supports multiple context sources:

- session context
- employee context
- organizational context
- tool-derived context
- private knowledge-base context

The system should distinguish between:

- transient execution context
- reusable working memory
- persistent business memory

This distinction matters for privacy, caching, and auditability.

---

## Initial domain roles

### Orchestrator Agent

Responsible for:

- top-level routing
- role selection
- policy enforcement
- lifecycle control

### Sales Agent

Connected to:

- the order and catalog systems of the application

Responsible for:

- sales-assistance workflows
- seller operations
- commercial execution within defined boundaries

### Data Agent

Connected to:

- the application's business data sources
- the application's internal knowledge base

Responsible for:

- retrieval and synthesis of business information
- controlled access to internal reference knowledge

### Summary Agent

Connected to:

- the application's team chat

Responsible for:

- summarizing meetings or conversations
- formatting outbound summaries
- communicating with employee-facing agents when needed

### Employee Agent

Connected to:

- employee identity
- optional local runtime
- knowledge-base access when permitted
- optional interaction with the Summary Agent

Responsible for:

- individual assistance
- coordination of employee-local context
- optional delegation under policy

---

## What the kit ships and what it leaves to you

### Shipped

The kit ships the core harness, the eight predefined roles, the OpenAI-compatible server and the reference backends. An agent built on the kit, a sales assistant for example, is a predefined role plus the tools of the application that uses it.

### Roadmap potential

The broader kit supports future extension toward:

- employee agents
- summaries
- deeper data assistance
- local workstation runtimes
- controlled child-agent delegation

These are kit capabilities or roadmap options; an application adopts them explicitly.

---

## Open decisions

The following decisions are still intentionally open:

1. Should agent definitions define only role semantics, or the full execution policy as well?
2. Can agents decide autonomously when to spawn child agents, or only under explicit system rules?
3. Which actions require human approval versus autonomous execution?
4. Which contexts are local-only, shared-team, or organization-wide?
5. What is the exact contract boundary between the kit and the application built on it?

---

## Recommended next documents

1. `docs/platform/role.md` already covers the agent definition folder schema.
2. `docs/architecture/delegation-policy.md`
   - rules for child-agent spawning and escalation
3. `docs/architecture/tool-permission-model.md`
   - RBAC and injection rules per connector
4. `docs/architecture/adr-002-agent-model-and-capabilities.md`
   - architecture corrections to the agent model, prompts, tool permissions, and persistence described in this document — includes the formal "agent" definition (`docs/platform/agent.md`) and records the correction of this document's folder-path references from `agents/` to `platform/roles/` (ADR-002 F.19)
