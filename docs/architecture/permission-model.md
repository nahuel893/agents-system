# Permission Model

## Employees as RBAC users

The platform models employees as RBAC users. Every agent runtime that acts on behalf of an employee is bound to that employee's identity and inherits their permission set — not a generic service account.

This means:
- An agent cannot perform an action that the associated employee is not permitted to perform
- Tool access is filtered by what the requesting user holds, not by what the agent's `manifest.md` declares alone
- Context loading is subject to privacy boundaries determined by the user's organizational role
- Delegation cannot be used to bypass RBAC controls

For system-triggered agents (scheduled jobs, callbacks from external systems), a service identity with an explicitly defined minimal permission set is used in place of an employee identity.

---

## Permission evaluation

Permissions are evaluated at two points in the lifecycle:

### At injection time

When the Capability Injector processes a runtime, it evaluates the requesting user's permission set against the requirements declared in each tool's `required_permissions` field. Any tool whose requirements are not met by the user's current permission set is excluded from the runtime's capability surface.

If a required tool (one the role cannot function without) is excluded, the runtime fails to instantiate. If an optional tool is excluded, it is silently omitted and the runtime proceeds with reduced capability.

This is the primary enforcement gate. Most permission decisions are made here.

### At execution time (revalidation)

For sensitive actions — those that write records, send messages, or modify organizational state — permissions are revalidated at the moment of execution. This guards against:

- Permission changes that occur between instantiation and execution in long-running sessions
- Delegated runtimes where the parent's cached permissions may have diverged from current state
- Warm-cached runtimes whose permission snapshot has become stale

The revalidation check is performed on the same RBAC source that was consulted at injection time. If the revalidation fails, the tool call is aborted and the situation is treated as an escalation trigger.

> **Open decision (3):** The threshold for what constitutes a "sensitive action" requiring revalidation has not been formally defined. Candidates include: any write operation, any outbound communication, any operation above a configured financial threshold, or any operation that is irreversible. The definition affects latency (revalidation is a round-trip) and operational complexity. A tiered approach (write = always revalidate, read = injection-time only) is a likely resolution, but it has not been decided.

---

## What permission filtering governs

| Capability | How permissions apply |
|---|---|
| **Tool access** | Each tool declares `required_permissions`. Tools whose requirements exceed the user's grants are excluded at injection time. |
| **Context loading** | Context sources are gated by scope (see Context privacy boundaries below). The manifest declares required context; the injector loads only what the user's permission set allows. |
| **Delegation** | The `delegation_policy.allowed` flag in the agent's `policy.md` is a necessary but not sufficient condition for delegation. The user must also hold delegation rights in their RBAC set. |

---

## Context privacy boundaries

The platform distinguishes three context scopes:

| Scope | Description |
|---|---|
| **local-only** | Context visible only to the agent instance and the associated user. Examples: a specific employee's conversation history, draft notes, personal session state. |
| **shared-team** | Context visible to agents operating on behalf of members of the same team or route. Examples: shared client records accessible to all preventistas on a route, team-level price list overrides. |
| **org-wide** | Context visible to all agents across the organization. Examples: the product catalog, public price lists, organizational policies. |

The agent's `manifest.md` (`context` field) and `policy.md` (`memory_policy.read_scope`) govern which scopes an agent runtime is permitted to access. The user's RBAC grants must also permit the scope.

> **Open decision (4):** The exact boundaries between `local-only`, `shared-team`, and `org-wide` context have not been formally specified for all context types. Specifically: conversation logs (local-only or accessible to supervisors?), client records (org-wide or route-scoped?), and order history (client-local or visible to all preventistas serving that client?). These boundaries have compliance and privacy implications and must be resolved before the platform handles sensitive business data in production.

---

## How permissions flow to child agents

When a runtime delegates to a child agent, the child receives its own independently evaluated permission set. The child's permissions are derived from:

1. The child role's manifest `permissions` field — what the child role requires
2. The requesting user's current RBAC grants — what the user is allowed to do

The intersection of these two sets is the child's effective permission set. Permissions held by the parent but not declared in the child manifest are not available to the child. The parent cannot grant the child permissions that exceed its own grants.

**Key rule:** Delegation cannot escalate permissions. A child agent can only act within the boundaries of what its own role declares AND what the user is authorized to do — whichever is more restrictive.

---

## Connector-specific injection rules

The following table defines which roles may inject which connectors, based on known platform connectors. "May inject" means the agent's `manifest.md` may declare the tool and the user must hold the required permission — both conditions must be satisfied.

| Connector | Tool(s) | Roles permitted to inject | Required permissions |
|---|---|---|---|
| **WhatsApp Business API** (Meta Cloud API) | `whatsapp_sender` | `preventa_agent` | `send:whatsapp` |
| **PostgreSQL / pgvector** | `rag_catalog_search` | `preventa_agent`, `data_agent` | `read:catalog` |
| **PostgreSQL** | `postgres_order_writer` | `preventa_agent` | `write:orders`, `write:order_items` |
| **PostgreSQL** | `client_lookup` | `preventa_agent`, `orchestrator_agent` | `read:client_registry` |
| **Redis** | `redis_session_state` | `preventa_agent`, `orchestrator_agent`, `employee_agent` | `read:session_state`, `write:session_state` |
| **Slack** | `slack_notifier` | `orchestrator_agent`, `summary_agent` | `send:slack` |
| **DeW / App Preventas** | `catalog_sync_reader`, `order_writer_dew` | `preventa_agent`, `data_agent` | `read:dew_catalog`, `write:dew_orders` |
| **App Sergio** | `sergio_data_reader` | `data_agent` | `read:sergio` |
| **Outline** | `wiki_reader` | `data_agent`, `employee_agent` | `read:outline` |
| **Element** | `meeting_transcript_reader` | `summary_agent` | `read:element` |

This table is illustrative of the permission model, not an exhaustive registry. New connectors added by a client delivery must define their required permissions and register them before any agent's `manifest.md` can reference them.

---

## Cross-references

- Agent permissions field: agent's `manifest.md` → `docs/platform/role.md` for schema
- Tool `required_permissions` field: `docs/platform/tool.md`
- Injection pipeline: `docs/platform/harness.md`
- Delegation permission constraints: `docs/architecture/delegation-policy.md`
