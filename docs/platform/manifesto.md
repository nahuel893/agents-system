# Platform Manifesto

## What this platform is

`agents-system` is a reusable agent runtime. It provides the infrastructure to build, deploy, and operate role-driven AI agents across different client domains without rewriting the runtime per deployment.

The platform is not a product for one client. It is the foundation on which client-specific products are built.

**Core responsibilities of the platform:**
- Declarative role definition via folder-based agent definitions
- Agent factory that resolves models, policies, and baseline configuration
- Capability injection pipeline: tools, skills, context, permissions, memory, and execution policies
- RBAC-aware permission enforcement bound to user/employee identity
- Lifecycle management: instantiate on demand, cache when beneficial, teardown cleanly
- Delegation and child-agent orchestration under explicit policy
- Audit trail and memory persistence

## What this platform is NOT

**Not a swarm.** Agents are not always-on processes running in parallel. They are instantiated on demand, execute a role, and are either destroyed or returned to a warm cache.

**Not a monolith.** The platform runtime and any specific client implementation are separate concerns. The platform ships no business logic, no company vocabulary, no integration credentials, and no domain-specific workflows. Those belong to the delivery scope.

**Not a hardcoded chatbot.** No behavior is burned into the runtime. All role behavior is assembled at instantiation time from the agent definition folder, injected capabilities, and policies. Changing a role does not require changing the runtime.

**Not a generic LLM wrapper.** The platform handles orchestration, delegation, RBAC, lifecycle, and auditability — concerns that a raw LLM API client does not address.

---

## Core principles

### 1. Declarative first

Agent roles are defined as folders under `agents/` — not as subclasses, not as configuration blobs, not as hardcoded prompt strings. Each folder contains `role.md` (identity), `manifest.md` (capabilities), and `policy.md` (behavior). The agent definition folder is the authoritative specification of what a role is allowed to be and do.

**Rationale:** Declarative definitions are readable by humans, auditable, version-controlled, and independent of the runtime implementation. They make the system inspectable without tracing code.

### 2. Instantiate on demand

Agents are created when a trigger arrives and destroyed (or cached) when execution completes. There is no permanent swarm by default.

**Rationale:** Always-on agents consume resources, accumulate stale context, and complicate permission enforcement. Demand-driven instantiation keeps the system lean and makes cold-start costs visible and measurable.

### 3. Dependency injection over hidden coupling

Every capability an agent uses — tools, skills, context, permissions, memory handles, orchestration policy — is injected explicitly at instantiation time. The runtime does not reach for global state.

**Rationale:** Hidden coupling makes behavior unpredictable and testing difficult. Explicit injection makes the capability surface of any running agent fully inspectable and auditable.

### 4. Least privilege

Every agent receives only the minimum set of capabilities required for the current role and the current task. No agent inherits capabilities it does not need, even if a parent agent has broader permissions.

**Rationale:** Least privilege limits blast radius when something goes wrong — whether a model behaves unexpectedly, a tool misbehaves, or a delegation chain is exploited. It also enforces the principle that agents should not be capable of more than their declared role requires.

### 5. Auditability

Every execution path must be reconstructible after the fact: who triggered it, which role was active, which tools were injected, which actions were taken, and which outputs were produced.

**Rationale:** Agents that take actions on behalf of users or organizations must be accountable. Auditability is a precondition for operating AI agents in a production business context, not an afterthought.

### 6. Composable delegation

Agents may delegate work to child agents only when policy explicitly permits it. Delegation is a capability that must be injected, not a default behavior that any agent can invoke freely.

**Rationale:** Unconstrained delegation creates unbounded execution graphs, unpredictable resource consumption, permission bypass risks, and audit gaps. Making delegation a governed capability keeps the system predictable and safe.

### 7. Performance-aware runtime

Agent instantiation (cold start) should be cheap enough that on-demand creation is the default. Where latency cannot tolerate cold start, a warm cache of pre-instantiated runtimes may be maintained — subject to strict context isolation rules.

**Rationale:** Performance concerns should not force architectural compromises. The warm cache is an optimization layer, not a structural requirement. It is safe only when stale permissions and stale private context cannot leak across users or sessions.

---

## Platform / client implementation boundary

The boundary between Core Platform and a client delivery scope is not a hard file boundary — it is a conceptual contract about what belongs where.

### Core Platform owns

- The agent runtime and its lifecycle
- The agent factory and model/provider resolution
- The capability injection pipeline and its ordering rules
- The RBAC model and permission evaluation logic
- The delegation and orchestration policy framework
- The audit trail and memory persistence interfaces
- The warm cache machinery and context isolation guarantees

The Core Platform ships no business logic, no company vocabulary, no integration credentials, and no domain knowledge. It provides infrastructure that is indifferent to which domain deploys it.

### Client delivery scope owns

- Agent definition folders that describe domain-specific agent behaviors (the full folder including `manifest.md` and `policy.md`)
- Tool definitions and connector configurations for the client's integrations
- Skills (behavioral packs and prompt modules) tuned for the client's domain and language
- Business rules, approval thresholds, vocabulary, and escalation policies
- Any credentials, secrets, or integration endpoints specific to the client

> **Open decision (5):** The exact contract boundary between platform IP and client-specific implementation has not been formalized. Specifically: which artifacts (agent definition folders, skill prompts, tool schemas) are platform-owned versus client-owned when the platform is deployed for a new client. See `docs/architecture/permission-model.md` for related discussion.

---

## Current delivery: BADIE

The first client implementation of this platform is the **Seller AI / Preventa agent** for **Distribuidora BADIE S.A.** (Grupo Manzur), an Argentine beverage distributor with brands including Quilmes, Brahma, Stella Artois, CCU, and Branca.

BADIE operates a network of field sellers called *preventistas* who visit retail points of sale (kiosks, convenience stores, bars, restaurants) and take orders manually. The Seller AI digitizes this process: points of sale can send orders via WhatsApp in colloquial Argentine Spanish, and the agent understands them, matches products via semantic search over the product catalog (RAG), and confirms and persists the order.

The BADIE delivery scope is intentionally narrow: the **Preventa Agent** is the only agent committed to the client. The platform's broader capabilities — employee agents, summary agents, data agents, local runtimes — are platform roadmap items, not client commitments.

The full BADIE delivery specification is in `docs/delivery/badie-seller-ai.md`.
