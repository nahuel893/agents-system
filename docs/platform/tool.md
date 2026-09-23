# Tool

## What a tool is

A tool is a connector to an external system or an executable capability that an agent runtime can invoke during execution. Tools are the mechanism by which agents interact with the world outside the model context: reading data, writing records, sending messages, or querying knowledge stores.

Tools are discrete, named, and registered in the platform. They are never available to a runtime by default — they must be declared in the agent's `manifest.md` and injected by the Capability Injector.

---

## Tool vs. skill

These two concepts are distinct and must not be conflated.

| Concept | What it is | Example |
|---|---|---|
| **Tool** | An executable connector to an external system or capability | `whatsapp_sender` sends a message via the Meta API |
| **Skill** | A behavioral pack or prompt module that shapes how an agent reasons | `colloquial_product_matching` teaches the agent how to interpret informal product references |

A tool *does something*. A skill *shapes how the agent thinks before doing something*. A tool produces a side effect or returns data. A skill has no side effect — it influences the model's reasoning process through injected prompt modules and context requirements.

---

## Tool definition schema

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | required | Unique identifier. Used by manifests and the injection pipeline to reference this tool. Snake-case (e.g., `catalog_search`). |
| `description` | `string` | required | What the tool does. Used by the agent runtime to select and invoke the tool correctly. Should be precise and unambiguous. |
| `connector` | `string` | required | The external system or service this tool connects to. Examples: `meta_whatsapp_api`, `postgres`, `redis`, `slack`. |
| `required_permissions` | `list[string]` | required | RBAC permission identifiers that must be present in the requesting agent's permission set before this tool can be injected. An agent lacking any required permission will not receive this tool. |
| `tier` | `T0 \| T1 \| T2 \| T3` | required | Capability tier (ADR-002 C.10): how dangerous the tool IS, independent of how `required_permissions` happens to be named. No default — a tool author must classify every tool explicitly. See "Capability tiers" below. |
| `inputs` | `object` | required | Named input parameters the tool accepts. Each entry: `name` (string), `type` (string), `required` (boolean), `description` (string). |
| `outputs` | `object` | required | Shape of the data the tool returns on success. Each entry: `name` (string), `type` (string), `description` (string). |
| `error_handling` | `object` | required | How the tool behaves on failure. Sub-fields: `on_connector_unavailable` (one of `fail_open`, `fail_closed`, `escalate`), `on_permission_denied` (one of `fail_closed`, `escalate`), `retries` (integer, 0 means no retry). |

---

## Capability tiers

ADR-002 C.10. Every tool declares a `tier` classifying how dangerous it IS, independent of how its `required_permissions` happen to be named. A tool author who names a dangerous permission without an `exec:`/`write:`/`send:` prefix is no longer invisible to the enforcement layers below — tier is an explicit, reviewable classification, not an inference from a naming convention.

| Tier | Meaning | Example | Who gets it |
|---|---|---|---|
| **T0** | Inherent — every agent needs it to function at all | session read | Every agent, via `base`/`agent` |
| **T1** | Scoped read | knowledge base lookup, sales report read | Roles whose manifest declares the corresponding `read:*` permission |
| **T2** | Scoped write/send | order writer, `send:message` | Always revalidated at call time |
| **T3** | Host execution | `use_term`, `read_file` | `operator-agent` branch only |

Two deterministic consequences follow from tier, both replacing (as a superset of, never a narrowing of) the older `write:`/`send:` prefix heuristic:

- **Interceptor Layer 2 (call time).** Any T2 or T3 tool is revalidated against current permissions at every call, regardless of how its `required_permissions` are named. `always_revalidate: true` extends the same revalidation to a specific T0/T1 tool without reclassifying it.
- **Capability Injector (build time), second barrier.** A role whose `policy.md` declares `untrusted_input: true` (ADR-002 C.11) never receives a T3 tool, even if the role's manifest and the requesting identity's grants would otherwise satisfy the tool's `required_permissions`. This is independent of C.11's `untrusted_input`/`exec:*` mutual-exclusion invariant: that invariant blocks by *permission family*, this barrier blocks by *tier*, so a T3 tool registered under a permission with no `exec:` prefix is still caught.

**Construction-time invariant.** Both of the above depend on `tier` actually reflecting a tool's danger — a tier is only as trustworthy as the author who set it. `ToolSpec` therefore validates itself at construction (`__post_init__`): any `required_permissions` entry in the `write:`/`send:` family (case-insensitive, whitespace-tolerant) requires `tier` to be `T2` or `T3`; any `exec:*` permission requires `tier=T3` specifically. A mismatch raises `ValueError` immediately, for every caller — production tool builders and test fixtures alike — so a tool author cannot silently under-tier a dangerous permission and have it slip past both enforcement layers above.

---

## How tools are registered

Tools are defined in the platform's tool registry. Each tool definition is a structured record (schema above) that the Capability Injector consults when building an agent runtime.

Registration makes a tool available to be injected. It does not grant any agent access to the tool. Access is governed by the agent's `manifest.md` (`tools` field) and the permission model (`required_permissions` field).

Tool registration is a platform-level operation. New tools introduced by a client delivery must be registered before they can appear in any agent's `manifest.md`.

---

## How tools are injected

The Capability Injector resolves tools as the first step in the injection pipeline (before skills, context, permissions, memory, and policies — see `docs/platform/harness.md` for the full ordering rationale).

**Injection sequence for each tool declared in the agent's `manifest.md`:**

1. Confirm the tool name exists in the registry. If not, fail instantiation.
2. If the requesting role's `policy.md` declares `untrusted_input: true` and the tool's `tier` is `T3`, exclude the tool — regardless of whether `required_permissions` would otherwise be satisfied (ADR-002 C.10's second barrier; see "Capability tiers" above).
3. Evaluate `required_permissions` against the requesting agent's permission set. If any required permission is absent, the tool is excluded. If the agent's `manifest.md` listed this tool as required, fail instantiation; if optional, skip silently.
4. Attach the tool's connector handle to the runtime's capability surface.
5. For sensitive tools (tier `T2` or `T3`) or any tool explicitly declared `always_revalidate: true`, mark the tool for revalidation at execution time. `always_revalidate` lets a T0/T1 tool opt into the same execution-time revalidation without reclassifying it — the escape hatch for a read that still needs to be checked at call time. Defaults to `false`.

> **Note on permission revalidation:** Permission checks at injection time reflect the state at the moment of instantiation. For actions with significant side effects — writing records, sending messages, modifying state — permissions are revalidated at the moment of execution, not only at injection time. This guards against permission changes that occur between instantiation and execution in long-running sessions. See `docs/architecture/permission-model.md`.

---

## Tool examples

### `whatsapp_sender`

| Field | Value |
|---|---|
| Connector | `meta_whatsapp_api` |
| Required permissions | `send:whatsapp` |
| Tier | `T2` (scoped send) |
| Inputs | `to` (string, E.164 phone number), `body` (string, message text) |
| Outputs | `message_id` (string), `status` (string) |
| Error handling | `on_connector_unavailable: fail_closed`, `on_permission_denied: escalate`, `retries: 1` |

Sends a text message to a WhatsApp contact via the Meta Cloud API. Fails closed on connector unavailability because a failed send is preferable to a silent drop that leaves the user expecting a reply that never arrives.

---

### `catalog_search`

| Field | Value |
|---|---|
| Connector | Deployment-supplied `CatalogSource` |
| Required permissions | `read:catalog` |
| Tier | `T1` (scoped read) |
| Inputs | `q` (string, natural-language catalog request) |
| Outputs | `results` (list of `{ sku, description, similarity }`, where `similarity` is a float or null for keyword fallback), `classification` (`direct`, `ambiguous`, or `no_match`) |

Searches the catalog through a deployment-supplied `CatalogSource`. The deployment owns its catalog schema and retrieval implementation; the public tool surface returns `results` and `classification`.

---

### `postgres_order_writer`

| Field | Value |
|---|---|
| Connector | `postgres` (tables: `orders`, `order_items`) |
| Required permissions | `write:orders`, `write:order_items` |
| Tier | `T2` (scoped write) |
| Inputs | `client_id` (integer), `items` (list of `{ sku, description, quantity, unit_price }`), `notes` (string, optional) |
| Outputs | `order_id` (integer), `status` (string) |
| Error handling | `on_connector_unavailable: fail_closed`, `on_permission_denied: fail_closed`, `retries: 0` |

Writes a confirmed order and its line items to the local database. Fails closed because creating a partial or duplicate order is worse than failing visibly.

---

### `redis_session_state`

| Field | Value |
|---|---|
| Connector | `redis` |
| Required permissions | `read:session_state`, `write:session_state` |
| Tier | `T2` (scoped write — `write:session_state` requires T2 or T3, enforced at construction) |
| Inputs | `operation` (one of `get`, `set`, `delete`), `key` (string), `value` (string, required for `set`), `ttl_seconds` (integer, optional) |
| Outputs | `value` (string or null) |
| Error handling | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0` |

Reads and writes ephemeral session state from Redis. Used for LangGraph conversation checkpointing and short-lived deduplication state. Fails open — session state loss degrades experience but does not corrupt business data.

---

### `client_lookup`

| Field | Value |
|---|---|
| Connector | `postgres` (table: `clients`) |
| Required permissions | `read:client_registry` |
| Tier | `T1` (scoped read) |
| Inputs | `phone_number` (string, E.164 format) |
| Outputs | `client_id` (integer), `name` (string), `price_list_id` (integer or null), `active` (boolean) |
| Error handling | `on_connector_unavailable: fail_open`, `on_permission_denied: fail_closed`, `retries: 0` |

Resolves a phone number to a registered client record. Fails open on connector unavailability, consistent with the platform's general policy that peripheral lookup failures should not block message processing.

---

## Cross-references

- Permission model and connector-specific injection rules: `docs/architecture/permission-model.md`
- Injection pipeline and ordering: `docs/platform/harness.md`
- Skill definitions (behavioral counterpart to tools): `docs/platform/skill.md`
- Agent `manifest.md` `tools` field: `docs/platform/role.md`
