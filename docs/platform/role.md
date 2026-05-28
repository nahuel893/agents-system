# Role

## What a role is

A role is a declarative behavioral identity. It defines what an agent is allowed to be and do within the platform — not a Python class, not a service, and not a prompt string.

A role is defined by a folder under `agents/`. The folder contains three files: `role.md` (identity), `manifest.md` (capabilities), and `policy.md` (behavior), plus an optional `skills/` subdirectory. The harness reads the agent definition folder at instantiation time and assembles the agent's capabilities from it.

**A role is not:**
- A live process or a thread
- A class to be subclassed per deployment
- A set of hardcoded instructions embedded in application code

---

## The AgentDefinition / AgentRuntime / Subsystem distinction

These three concepts are distinct and must not be conflated.

| Concept | Meaning | Where it lives |
|---|---|---|
| **AgentDefinition** | Folder-based declarative definition of a role (`role.md` + `manifest.md` + `policy.md`) — what the role is allowed to be and do | Folder on disk / version control |
| **AgentRuntime** | Live in-memory execution instance assembled from an agent definition at trigger time | Memory, exists only during execution |
| **Subsystem** | Coordinated set of roles and policies within a domain | Configuration / topology definition |

An AgentDefinition can be instantiated many times, each producing a separate AgentRuntime. A Subsystem groups related roles and governs how they interact — but a Subsystem is not a process; it is a policy boundary.

---

## Agent definition principle

> The agent definition defines **what the role is allowed to be and do**.
> The runtime decides **how and when it is instantiated**.

The agent definition declares capabilities, permissions, and constraints. The runtime decides whether to cold-start or reuse a warm cache, which model to invoke, and how to execute the role given the live context.

---

## Agent definition schema

An agent definition is a folder under `agents/` containing three files. Fields marked **required** must be present for the definition to be valid.

### `role.md` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | required | Unique identifier for the role. Used by the factory to select and instantiate the role. Snake-case recommended (e.g., `preventa_agent`). |
| `version` | `string` | optional | Semantic version of the role definition. Useful for audit trails and cache invalidation. |
| `purpose` | `string` | required | One to three sentences describing what this role exists to accomplish. Not a technical description — a behavioral statement. |
| `scope` | `string` | required | The operational boundary. Which domain, which users, and which tasks this role is authorized to act on. |

### `manifest.md` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `tools` | `list[string]` | required | Names of tools this role is permitted to use. The platform injects only tools listed here. Any tool not listed is unavailable to this runtime, even if it exists in the registry. |
| `skills` | `list[string]` | optional | Names of skill packs this role accepts. Skills shape how the agent reasons and responds. See `docs/platform/skill.md`. |
| `context` | `object` | required | Context requirements. Specifies what context the runtime must receive at injection time. Sub-fields: `session` (boolean), `user_identity` (boolean), `org_context` (boolean), `private_wiki` (boolean), `tool_derived` (list of tool names whose outputs are required as context). |
| `permissions` | `list[string]` | required | RBAC permission identifiers required for this role to operate. The platform evaluates these at injection time against the requesting user's permission set. See `docs/architecture/permission-model.md`. |

### `policy.md` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `autonomy` | `string` | required | Autonomy level for this role. One of `full`, `supervised`, or `confirm`. See `docs/platform/policy.md`. |
| `escalation_rules` | `object` | required | Conditions under which this role must escalate to a human or to a higher-authority agent. Sub-fields: `escalate_to` (role name or `human`), `conditions` (list of trigger conditions as strings). |
| `delegation_policy` | `object` | required | Whether this role may delegate to child agents, and under what constraints. Sub-fields: `allowed` (boolean), `permitted_child_roles` (list of role names, empty if `allowed: false`), `max_depth` (integer). See `docs/architecture/delegation-policy.md`. |
| `memory_policy` | `object` | required | Governs what the runtime may read from and write to memory. Sub-fields: `read_scope` (one of `local`, `team`, `org`), `write_scope` (one of `local`, `team`, `org`), `persist_conversation` (boolean). |
| `audit_policy` | `object` | required | What the runtime must record in the audit trail. Sub-fields: `log_tool_calls` (boolean), `log_delegations` (boolean), `log_escalations` (boolean), `retention_days` (integer or `null` for platform default). |

> **Open decision (1):** Should role definitions define only role semantics — purpose, scope, tools, skills, escalation rules — or also execution policy, such as which model to use, whether to enable warm caching, and what execution timeouts to apply? Execution policy may belong in `policy.md` (coupling role and execution), in the factory (separating concerns), or in a separate platform-level policy layer. This decision affects whether agent definitions are portable across different runtime configurations.

---

## Example agent definition: Preventa Agent

The Preventa Agent definition lives at `agents/preventa/` and consists of three files.

**`agents/preventa/role.md`**

```markdown
# Role: preventa_agent

## purpose
Assist field sellers (preventistas) in receiving, understanding, and confirming
product orders from retail points of sale via WhatsApp, using the client's
product catalog and price list.

## scope
- Domain: sales order intake for Distribuidora BADIE S.A.
- Users: registered WhatsApp contacts mapped to active clients in the client registry
- Tasks: interpret colloquial product requests, match to catalog via RAG, confirm
  and persist orders
```

**`agents/preventa/manifest.md`**

```markdown
## tools
- whatsapp_sender
- rag_catalog_search
- postgres_order_writer
- redis_session_state
- client_lookup

## skills
- order_extraction
- colloquial_product_matching
- confirm_flow

## context
  session: true
  user_identity: true
  org_context: false
  private_wiki: false
  tool_derived:
    - client_lookup

## permissions
  - read:catalog
  - read:client_registry
  - write:orders
  - write:order_items
  - read:price_lists
  - send:whatsapp
```

**`agents/preventa/policy.md`**

```markdown
## autonomy
  level: supervised

## escalation_rules
  escalate_to: human
  conditions:
    - client is not registered (active=false)
    - order total exceeds configured approval threshold
    - ambiguous product match after two clarification rounds
    - explicit request from client to speak with a human

## delegation_policy
  allowed: false
  permitted_child_roles: []
  max_depth: 0

## memory_policy
  read_scope: local
  write_scope: local
  persist_conversation: true

## audit_policy
  log_tool_calls: true
  log_delegations: false
  log_escalations: true
  retention_days: 90
```

This agent definition folder defines the Preventa Agent's behavioral boundary. At instantiation time the platform injects exactly the five tools listed in `manifest.md`, the three skills listed, the session and user identity context, and validates that the requesting user holds all six permissions. The agent cannot delegate (`policy.md` sets `allowed: false`), so no orchestration policy is injected. Any condition in `policy.md`'s `escalation_rules` terminates the current execution path and hands control to a human operator.

---

## Cross-references

- Tool definitions: `docs/platform/tool.md`
- Skill definitions: `docs/platform/skill.md`
- Runtime lifecycle and injection order: `docs/platform/harness.md`
- Delegation rules: `docs/architecture/delegation-policy.md`
- Permission model: `docs/architecture/permission-model.md`
