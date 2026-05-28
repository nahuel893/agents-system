# Deployment Model

The platform separates generic role definitions from client-specific implementations through a two-layer structure. Generic roles define the behavioral template and capability boundaries. Deployments extend and specialize those roles for a specific client context.

This separation is the resolution of open decision #5: platform IP lives in `platform/roles/`. Client-specific implementation lives in `deployments/{client}/`.

---

## Folder structure

```
platform/
  roles/
    sales-agent/          ← generic: what a sales agent is
      role.md
      manifest.md
      policy.md
    orchestrator/
      role.md
      manifest.md
      policy.md
    data-agent/
      role.md
      manifest.md
      policy.md
    summary-agent/
      role.md
      manifest.md
      policy.md

deployments/
  badie/
    sales-agent/          ← BADIE override: preventista + DeW + colloquial AR Spanish
      role.md
      manifest.md
      policy.md
      skills/
        order_extraction.md
        colloquial_matching.md
        confirm_flow.md
  other-client/
    sales-agent/          ← different override: different language, tools, policy
      role.md
      manifest.md
```

---

## Merge semantics

When the harness instantiates an agent for a deployment, it builds the final definition in two steps:

1. Load the generic role from `platform/roles/{role-type}/`
2. Merge the client override from `deployments/{client}/{role-type}/`
3. Build the runtime from the merged definition

The override follows these rules per file:

### `role.md` override

The deployment `role.md` extends the generic role with client-specific context:
- Adds company name, domain, language, and business vocabulary
- Adds client-specific purpose statement on top of the generic one
- Cannot remove or contradict the generic role's scope boundaries

### `manifest.md` override

The deployment `manifest.md` can:
- Add tools from the approved platform registry (e.g., `dew_connector`, `app_preventas_writer`)
- Declare which skills from the deployment's `skills/` folder are active
- Restrict context scope (narrower than generic)

The deployment `manifest.md` cannot:
- Add tools not present in the platform registry
- Elevate the permission requirements of any tool
- Expand context access beyond what the generic manifest allows

### `policy.md` override

The deployment `policy.md` can:
- Restrict autonomy level (e.g., `supervised` → `confirm`)
- Add stricter escalation rules
- Reduce execution limits (shorter timeouts, fewer tool calls)
- Define client-specific human-in-the-loop thresholds

The deployment `policy.md` cannot:
- Elevate autonomy level beyond the generic role's ceiling
- Remove escalation rules defined in the generic policy
- Increase execution limits beyond platform defaults

### `skills/` (deployment-only)

Skills are always client-specific. There are no generic skills. The `skills/` folder exists only in deployments. Skills are behavioral prompt modules that shape how the agent reasons about domain-specific tasks — they encode client vocabulary, business rules, and interaction patterns.

---

## Permission invariant

> A deployment can only restrict or specialize. It can never elevate.

The generic role defines the maximum capability surface. No deployment override can exceed it. The Capability Injector enforces this at injection time: if a deployment manifest requests a tool or permission not present in the generic role's allowed surface, the injection fails and the runtime is not created.

---

## The harness merge algorithm

```
function build_runtime(client, role_type, user_identity):
  generic = load_folder("platform/roles/{role_type}/")
  override = load_folder("deployments/{client}/{role_type}/")  # may not exist

  if override is None:
    definition = generic
  else:
    definition = merge(generic, override)
    assert definition.permissions ⊆ generic.permissions  # invariant
    assert definition.autonomy_level ≤ generic.autonomy_level  # invariant

  return AgentFactory.build(definition, user_identity)
```

If no deployment override exists for a given client and role, the platform uses the generic definition as-is. This allows gradual specialization — a client can start with the generic role and add overrides incrementally.

---

## Example: BADIE sales-agent

### `platform/roles/sales-agent/role.md` (generic)
```
name: sales-agent
purpose: >
  Assist customers in placing orders through a conversational interface.
  Understand product requests in natural language, match them against the
  available catalog, confirm the order, and persist it.
scope: order-taking, catalog-lookup, order-confirmation
```

### `deployments/badie/sales-agent/role.md` (override)
```
extends: platform/roles/sales-agent
company: Distribuidora BADIE S.A. (Grupo Manzur)
language: es-AR (Rioplatense Spanish)
domain: beer and beverage distribution — Argentina
vocabulary:
  - "la rubia" → Quilmes
  - "cajón" → case of 24 units
  - "preventista" → field sales representative
  - "punto de venta" → retail client (kiosk, bar, restaurant)
purpose_extension: >
  Handle orders from registered retail clients (puntos de venta) via
  WhatsApp Business API. Interpret colloquial Argentine product names
  and quantities, match them against the BADIE catalog using RAG, and
  persist confirmed orders to the DeW / App Preventas system.
```

---

## Memory scoping in deployments

The memory layer follows the same two-level scope:

| Scope | What it stores |
|---|---|
| `platform / {role_type}` | Generic agent behavior learned over time across all deployments |
| `deployment / {client} / {role_type}` | Client-specific knowledge (catalog patterns, client preferences) |
| `deployment / {client} / {role_type} / {user_id}` | Individual user memory (order history, preferences, delivery notes) |

A deployment's memory is always isolated from other deployments. Cross-client memory access is never permitted.

---

## Cross-references

- Role definition schema: `docs/platform/role.md`
- Policy schema and enforcement: `docs/platform/policy.md`
- Capability injection and merge enforcement: `docs/platform/harness.md`
- Permission model and RBAC: `docs/architecture/permission-model.md`
- BADIE delivery scope: `docs/delivery/badie-seller-ai.md`
