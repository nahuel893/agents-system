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
  acme/
    sales-agent/          ← ACME override: preventista + DeW + colloquial AR Spanish
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
- Increase execution limits beyond platform defaults. A `null` value counts as the platform default and is checked like any other value; a value that is not a finite, non-negative number (`NaN`, infinity, a negative value, a boolean) raises `DefinitionError`

### `skills/`

Skills are behavioral prompt modules that shape how the agent reasons about domain-specific tasks — they encode client vocabulary, business rules, and interaction patterns. Skill content resolves from three sources, checked in this order:

1. **Inline Python content** — a custom agent built with `Agent(skill_contents={...})` (or `Agent.from_folder(path, skill_contents={...})`) supplies skill content directly, with no file read.
2. **The custom agent's own folder** — `Agent.from_folder(path)`'s own `skills/<name>.md`, for a custom agent that ships its own default skill content.
3. **The deployment** — `deployments/{client}/{role}/skills/<name>.md`. This is the **only** source a predefined platform role ever resolves skills from; a predefined role has no folder or inline source of its own.

A declared skill that resolves from none of the three sources fails loud (`FactoryError`). A custom agent may combine its own two sources — e.g. ship a folder default and let an inline override (source 1) replace one skill by name. The deployment source is exclusively a predefined-role mechanism: pairing a `client` deployment override with a folder- or inline-sourced agent (any `Agent`/`Agent.from_folder` locator) raises `DefinitionError` at resolve time, since a custom agent has no deployment tree to look one up in.

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

## Example: ACME sales-agent

### `platform/roles/sales-agent/role.md` (generic)
```
name: sales-agent
purpose: >
  Assist customers in placing orders through a conversational interface.
  Understand product requests in natural language, match them against the
  available catalog, confirm the order, and persist it.
scope: order-taking, catalog-lookup, order-confirmation
```

### `deployments/acme/sales-agent/role.md` (override)
```
extends: platform/roles/sales-agent
company: a regional beverage distributor
language: es-AR (Rioplatense Spanish)
domain: beer and beverage distribution — Argentina
vocabulary:
  - "la rubia" → BrandA
  - "cajón" → case of 24 units
  - "preventista" → field sales representative
  - "punto de venta" → retail client (kiosk, bar, restaurant)
purpose_extension: >
  Handle orders from registered retail clients (puntos de venta) via
  WhatsApp Business API. Interpret colloquial Argentine product names
  and quantities, match them against the ACME catalog using RAG, and
  persist confirmed orders to the DeW / App Preventas system.
```

---

## Serving: registering runtimes with `create_app`

An application registers what it serves when it builds the app (ADR-004). Each runtime id is the application's own choice, and it is opaque: nothing parses it for a role or a deployment.

```python
app = create_app(
    registry_factory=build_registry,
    roots=RootConfig(deployments_root=Path("deployments")),
    agents={
        "acme-sales": "sales-agent",  # a predefined role
        "triage-bot": Agent.from_folder("agents/triage-bot"),  # a custom agent
    },
    clients={"acme-sales": "acme"},  # deployments/acme/sales-agent/ narrows it
    grants={
        "acme-sales": ["read:catalog", "write:orders"],
        "triage-bot": ["read:catalog"],
    },
)
```

- **Every registered id is built at boot.** One entry that fails to resolve or equip fails the whole boot; no runtime is served while another is dropped.
- **An id** is 1-64 letters, digits, `_` or `-`, starting with a letter or digit.
- **`clients`** is valid only for a predefined role registered by name. It fails boot for an `Agent` entry, for an id `agents` does not register, and without `agents`: a client names a subtractive override, and a silently ignored one would serve the agent without its narrowing. A client override needs an explicit `RootConfig(deployments_root=...)`.
- **`grants`** is the explicit deploy-time grant, one list per registered id. Nothing is granted automatically: an id with no entry fails boot, and so does a bare string in place of a list. Without `grants=`, `DEPLOY_GRANTS` is the source, keyed by the same ids. See `docs/architecture/permission-model.md`.
- **Channels look ids up.** `WHATSAPP_RUNTIME_ID` and every `ADAPTER_RUNTIMES` id must be a registered id, or boot fails naming it. `/v1/models` lists only the `ADAPTER_RUNTIMES` ids, never every registered one.

Without `agents=`, `create_app` keeps the Settings-driven boot: `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID` carry `{deployment}__{role}` ids (`_generic__{role}` for no deployment).

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
- ACME delivery scope: `docs/delivery/acme-seller-ai.md`
