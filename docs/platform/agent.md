# Agent

This document is the platform's reference definition of "agent." Every other
document that uses the word — `manifesto.md`, `role.md`, `policy.md`,
ADR-002 — defers to this one. Where this file and an older document disagree,
this file wins; the disagreement is recorded as a correction in ADR-002.

---

## General definition

> An **agent** is a model that decides its own actions in a loop: it chooses
> what to do, acts through tools, observes the result, and repeats until the
> goal is met or a limit is hit.

The definitional weight sits on "decides its own actions." A system that
executes a fixed sequence of steps — even if an LLM writes the text at each
step — is not an agent under this definition unless the *next step itself* is
a decision the model makes from the current state, not a step a human or a
static graph already chose for it.

## Chatbot vs. LLM workflow vs. agent

| | Who decides the next step | Does it act on the world |
|---|---|---|
| **Chatbot** | The human, every turn. The model only replies. | No. Output is text back to the user; nothing is called, written, or sent. |
| **LLM workflow** | The workflow author, at design time. A fixed pipeline (e.g. classify → retrieve → summarize) may call an LLM at each stage, but the *sequence* of stages is hardcoded, not chosen by the model at runtime. | Sometimes — a workflow stage may call a tool, but which stage runs next is not a model decision. |
| **Agent** | The model, at run time, from the current state. It chooses which tool to call next (or none), given what it has observed so far. | Yes — that is the point. Tool calls are how it acts, and the loop exists so it can observe the effect and decide again. |

The platform's own connector layer (harness/injector.py, harness/interceptor.py)
exists specifically to bound *what* an agent's runtime decisions are allowed to
touch — it does not change the fact that the model, not a workflow author, is
choosing.

## Five necessary components

| # | Component | Without it | STATUS |
|---|---|---|---|
| 1 | **Objective & identity** | The model has nothing to decide *toward*. A role's `role.md` purpose/scope is the objective; see "Role = class" below for identity. | ✅ implemented (role); ❌ not implemented (per-principal identity — see ADR-002 §A.2) |
| 2 | **A model that decides** | Nothing chooses the next action; you have a script, not an agent. `AgentRuntime` binds a `BaseChatModel` and lets it choose tool calls (`src/agentsys/agent/graph.py:344-375`). | ✅ implemented |
| 3 | **Tools** | Without tools the loop can still run, but the model can only talk — it cannot act on the world. This is a real, supported edge case on this platform, not a hypothetical: `AgentRuntime.__init__` only calls `model.bind_tools(...)` when the granted tool surface is non-empty (`agent/graph.py:373-375` — "Only call bind_tools when there are tools to bind — some fake models raise NotImplementedError for bind_tools even with an empty list"). A role resolved with zero granted tools degrades to a chatbot at runtime, silently, by this exact code path. | ✅ implemented, including the tool-less edge case |
| 4 | **A loop with observation** | A single model call with no chance to see the result of its own action cannot correct course. `_build_graph` wires `_call_model → _execute_tools → _call_model → …` until the model stops requesting tools or a limit fires. | ✅ implemented |
| 5 | **Limits** | An unbounded loop is a runaway cost and safety risk. `PLATFORM_DEFAULT_LIMITS` (`harness/loader.py:139-145`): `tool_call_timeout_s=10`, `total_execution_timeout_s=60`, `max_tool_calls=20`, `max_delegation_depth=2`, `max_clarification_attempts=3`. `_effective_limits` (`agent/graph.py:58-73`) merges a role's overrides over these per-key; `_ENFORCED_LIMIT_KEYS` (`agent/graph.py:51-55`) is the subset the loop itself actually reads today — `max_tool_calls`, `total_execution_timeout_s`, `tool_call_timeout_s`. | ⚠️ partial — the three enforced keys work; `max_delegation_depth` and `max_clarification_attempts` are declared and merged but not read by the loop (`agent/graph.py:50` says so explicitly: "not yet read here") |

## Platform formula

> **Agent = Role + Identity + Model, run in a bounded, audited loop.**

Role supplies the objective, the tool surface, and the policy. Identity
supplies who the agent is acting for (§ Role = class below). Model supplies
the decision-making. "Bounded" is component 5; "audited" is the
`audit_event` pipeline (`docs/platform/audit.md`) recording every tool
grant/denial/call.

| Formula term | Platform mechanism | STATUS |
|---|---|---|
| Role | `AgentDefinition`, resolved from `platform/roles/<role>/{role.md,manifest.md,policy.md}` by `resolve()` (`harness/loader.py:1035`) | ✅ implemented |
| + Identity | `granted_permissions` parameter of `build_runtime` — see ADR-002 §A.2 for why this is currently the role's own permissions, not a principal's | ⚠️ partial — mechanism exists, no principal feeds it |
| + Model | Any `langchain_core.BaseChatModel`, bound to the granted tool surface in `AgentRuntime.__init__` (`agent/graph.py:363-375`) | ✅ implemented |
| , run in a | `EquippedRuntime` (`harness/factory.py:68-83`) is the assembled, not-yet-live spec; `AgentRuntime.run_turn` (`agent/graph.py:394-442`) is the execution | ✅ implemented |
| bounded | `PLATFORM_DEFAULT_LIMITS` + `_effective_limits` (above) | ⚠️ partial (see component 5) |
| , audited | Layer 1 injector emits `tool_granted`/`tool_denied`/`unknown_tool` (`harness/injector.py:77-144`); Layer 2 interceptor emits `tool_call_attempted`/`tool_call_blocked`; both via `audit/events.py` | ✅ implemented |
| loop | `_build_graph` / `_call_model` / `_execute_tools` (`agent/graph.py`) | ✅ implemented |

## Key distinction: role = class, agent = instance

A **role** is a blueprint: `platform/roles/sales-agent/` declares what any
agent instantiated with that role is allowed to be and do. It is not running
anything and is not acting on behalf of anyone. `docs/platform/role.md` states
this correctly already: "The agent definition defines what the role is
allowed to be and do. The runtime decides how and when it is instantiated."

An **agent** is an instance of a role acting on behalf of a specific
principal (a customer, an employee, a service identity). Two different
employees each talking to their own "employee agent" (`docs/architecture/agent-platform.md:154-163`,
already describes this: "one main employee agent... scoped to the employee
identity and permissions") are two different agents, both instances of the
same role.

Today the platform resolves roles correctly but does not carry a principal
identity through to the runtime (ADR-002 §A.2): `granted_permissions` at
startup is `definition.permissions` — the role's own declared permissions,
not any employee's or customer's (`src/agentsys/main.py:326-333`,
specifically line 329). Concretely: today, every WhatsApp customer talking to
the `sales-agent` role is served by the *same* `AgentRuntime` object, built
once at boot (`main.py:334-336`) and cached in `app.state.runtimes`, with the
same permission grant. There is no code representation of "this agent, for
this customer" distinct from "this role." The class/instance distinction is
implemented for roles and not yet implemented for agents.

## Target model: the agent as a virtual actor

The platform does not yet implement this section — it is the direction ADR-002
sets, modeled after the virtual-actor pattern (as in Microsoft Orleans, or
Cloudflare Durable Objects): an addressable unit of identity + state that the
runtime activates on demand and never has to think of as "a process."

> **An agent always exists in durable storage; it is activated in memory only
> while serving, and every step is persisted.**

Concretely, this means splitting what is durable from what is ephemeral:

- **Durable** (lives in persistent storage, survives every process restart):
  the agent's identity — `agent_id`, `role`, `principal` — and its memory
  space (working memory, agent-own memory; see below). This is the
  authoritative state. A process that loses it has not "reset an agent," it
  has destroyed one.
- **Ephemeral** (exists only while serving a request): the `AgentRuntime`
  Python object itself. It is *activated* — constructed from the durable
  identity plus the current `EquippedRuntime` for that role — when a message
  arrives, used for exactly one `run_turn`, and then discarded. The in-memory
  object is never the source of truth for anything; if it disappears between
  turns, nothing is lost, because every step that matters was already
  persisted before the object went away.

### Why a literal long-lived in-process object fails

The tempting alternative — keep one `AgentRuntime` object alive per user for
as long as they're active, in process memory — breaks under conditions this
platform already has or is explicitly aiming for:

- **A process restart loses the object and everything it held in memory that
  wasn't separately persisted.** ADR-001 already establishes that working
  memory (the Redis checkpointer) is the one piece of state that does survive
  a restart today, precisely *because* it lives outside the process. Anything
  kept only on the Python object — including "which `AgentRuntime` instance
  belongs to this user" — does not survive.
- **Multiple uvicorn workers or multiple server processes split the same
  user across processes.** ADR-001 §2 ("Stage B — target, 100 concurrent
  conversations: multiple processes") is the concrete trigger: the moment
  there is more than one process, "the in-process object for user X" stops
  being a well-defined thing — which process has it? A load balancer does not
  know or care, and pinning a user to a process (sticky sessions) reintroduces
  the exact kind of shared, stateful, per-process coupling ADR-001 spent its
  whole analysis removing.
- **1,000 idle users would mean 1,000 live objects sitting in memory doing
  nothing** — exactly the "always-on swarm" the manifesto explicitly rejects
  ("Not a swarm... They are instantiated on demand... There is no permanent
  swarm by default," `docs/platform/manifesto.md:20,40`). A durable identity
  with an ephemeral activation gets the same addressability (any request for
  agent X can find agent X) without paying to keep 1,000 idle Python objects
  resident.

### Activation is cheap

Two agents with the same role and the same effective permission set can
share the same *stateless* `EquippedRuntime` — the assembled tool surface,
system prompt, and skill set are identical for both, because none of that
depends on which principal is asking. Only identity (`agent_id`, `principal`)
and memory are per-agent. This is why activation can be cheap: building an
`AgentRuntime` around an already-built `EquippedRuntime` is binding a model to
an existing tool schema (`agent/graph.py:373-375`), not re-resolving a role
from disk. The manifesto's own performance principle already anticipates
this: "Agent instantiation (cold start) should be cheap enough that on-demand
creation is the default" (`docs/platform/manifesto.md:69`) — cheap
per-activation cost is what makes durable-identity-plus-ephemeral-activation
viable instead of a performance liability.

## Three kinds of memory

| Kind | What it holds | Isolation | STATUS |
|---|---|---|---|
| **Working memory** | The current conversation's message history — what lets a multi-turn exchange stay coherent within one thread. | By `thread_id` | ⚠️ partial — the LangGraph Redis checkpointer holds this, keyed by `thread_id` (a customer's phone number in WhatsApp, `webhook.py:246`, when `whatsapp_checkpointer_enabled`). See ADR-002 §D.4/G for its durability gap (no persistence backing Redis) and TTL. |
| **Agent-own long-term memory** | Durable facts *about this agent's principal*, accumulated and curated across conversations — not the raw transcript, but what the agent decided was worth keeping ("this customer prefers delivery on Tuesdays"). | By `agent_id` | ⏳ pending — does not exist. No `remember`/`recall` tool, no storage, no schema. See ADR-002 §G.25. |
| **Organization-shared knowledge** | Facts the organization holds that are not any one agent's private memory — the product catalog, policy documents, a wiki. Not owned by the agent; the agent *queries* it via a tool. | By org, gated by the querying agent's permissions | ⚠️ partial — `KnowledgeBase` (`services/knowledge.py:23`) is a `Protocol` port with no concrete implementation shipped anywhere in the repository, not even a test fake; `platform_connectors.py` binds it as a fail-closed tool ("no knowledge base is available on this deployment") when nothing is wired in. |

**Isolation by `agent_id` is a platform-enforced boundary, never something
the model chooses.** This mirrors the permission model's own rule for tool
access (`docs/architecture/permission-model.md` — filtering happens at
injection time, not by the model deciding to respect a boundary) and is the
reason agent-own memory, once implemented, must be gated the same way
`memory_policy.read_scope`/`write_scope` already declare intent to gate it
(ADR-002 §A.5): declaratively, at the platform layer, not by prompting the
model to behave.

## Cross-references

- Role definition schema: `docs/platform/role.md`
- Runtime pipeline (trigger → tool execution): `docs/platform/harness.md`
- Policy (autonomy, limits, enforcement layers): `docs/platform/policy.md`
- Permission model and RBAC: `docs/architecture/permission-model.md`
- The architecture corrections this definition is grounded in: `docs/architecture/adr-002-agent-model-and-capabilities.md`
