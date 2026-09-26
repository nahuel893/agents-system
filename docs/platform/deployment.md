# Deployment Model

The platform separates predefined role definitions from client-specific implementations through a two-layer structure. Predefined roles define the behavioral template and capability boundaries. Deployments extend and specialize those roles for a specific client context.

This separation is the resolution of open decision #5: platform IP lives in `platform/roles/`. Client-specific implementation lives in `deployments/{client}/`.

---

## Folder structure

```
platform/
  roles/
    sales-agent/          ← predefined: what a sales agent is
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

1. Load the predefined role from `platform/roles/{role-type}/`
2. Merge the client override from `deployments/{client}/{role-type}/`
3. Build the runtime from the merged definition

The override follows these rules per file:

### `role.md` override

The deployment `role.md` extends the predefined role with client-specific context:
- Adds company name, domain, language, and business vocabulary
- Adds client-specific purpose statement on top of the predefined one
- Cannot remove or contradict the predefined role's scope boundaries

### `manifest.md` override

The deployment `manifest.md` can:
- Add tools from the approved platform registry (e.g., `dew_connector`, `app_preventas_writer`)
- Declare which skills from the deployment's `skills/` folder are active
- Restrict context scope (narrower than predefined)

The deployment `manifest.md` cannot:
- Add tools not present in the platform registry
- Elevate the permission requirements of any tool
- Expand context access beyond what the predefined manifest allows

### `policy.md` override

The deployment `policy.md` can:
- Restrict autonomy level (e.g., `supervised` → `confirm`)
- Add stricter escalation rules
- Reduce execution limits (shorter timeouts, fewer tool calls)
- Define client-specific human-in-the-loop thresholds

The deployment `policy.md` cannot:
- Elevate autonomy level beyond the predefined role's ceiling
- Remove escalation rules defined in the predefined policy
- Increase execution limits beyond platform defaults. A `null` value counts as the platform default and is checked like any other value; a value that is not a finite, non-negative number (`NaN`, infinity, a negative value, a boolean) raises `DefinitionError`

### `skills/`

Skills are behavioral prompt modules that shape how the agent reasons about domain-specific tasks — they encode client vocabulary, business rules, and interaction patterns. Skill content resolves from three sources, checked in this order:

1. **Inline Python content** — a custom agent built with `Agent(skill_contents={...})` (or `Agent.from_folder(path, skill_contents={...})`) supplies skill content directly, with no file read.
2. **The custom agent's own folder** — `Agent.from_folder(path)`'s own `skills/<name>.md`, for a custom agent that ships its own default skill content.
3. **The deployment** — `deployments/{client}/{role}/skills/<name>.md`. This is the **only** source a predefined platform role ever resolves skills from; a predefined role has no folder or inline source of its own.

A declared skill that resolves from none of the three sources fails loud (`FactoryError`). A custom agent may combine its own two sources — e.g. ship a folder default and let an inline override (source 1) replace one skill by name. The deployment source is exclusively a predefined-role mechanism: pairing a `client` deployment override with a folder- or inline-sourced agent (any `Agent`/`Agent.from_folder` locator) raises `DefinitionError` at resolve time, since a custom agent has no deployment tree to look one up in.

Both file sources are contained: a skill file must resolve, after `..` and symlinks, inside its own `skills/` folder and inside that folder's root (the importer root for source 2, the deployments root for source 3). A skill or role name with `..` in it, or a symlink leading out, is treated as not found. See `role.md`, "Importer folders stay inside their root".

---

## Permission invariant

> A deployment can only restrict or specialize. It can never elevate.

The predefined role defines the maximum capability surface. No deployment override can exceed it. The Capability Injector enforces this at injection time: if a deployment manifest requests a tool or permission not present in the predefined role's allowed surface, the injection fails and the runtime is not created.

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

If no deployment override exists for a given client and role, the platform uses the predefined definition as-is. This allows gradual specialization — a client can start with the predefined role and add overrides incrementally.

---

## Example: ACME sales-agent

### `platform/roles/sales-agent/role.md` (predefined)
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
- **`clients`** is valid only for a predefined role registered by name. It fails boot for an `Agent` entry, for an id `agents` does not register, without `agents`, and for a value that is not a client name (a `str` of letters, digits, `_` or `-`, starting with a letter or digit): a client names a subtractive override, and a silently ignored one would serve the agent without its narrowing. A `None` value, such as an unset environment variable, is not "no client"; leave the id out of `clients` instead. A client override needs an explicit `RootConfig(deployments_root=...)`.
- **`grants`** is the explicit deploy-time grant, one list per registered id. Nothing is granted automatically: an id with no entry fails boot, and so does a bare string in place of a list. Without `grants=`, `DEPLOY_GRANTS` is the source, keyed by the same ids. See `docs/architecture/permission-model.md`.
- **Channels look ids up.** `WHATSAPP_RUNTIME_ID` and every `ADAPTER_RUNTIMES` id must be a registered id, or boot fails naming it. `/v1/models` lists only the `ADAPTER_RUNTIMES` ids, never every registered one.

### Without `agents=`: `AGENT_REGISTRATIONS`

Without `agents=`, `create_app` registers the entries of the `AGENT_REGISTRATIONS` environment variable instead, and builds them through the same rules. It maps each runtime id to a predefined role, as `"{role}"` or `"{role}@{client}"`:

```bash
AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme", "support": "sales-agent"}'
ADAPTER_RUNTIMES='["acme-sales", "support"]'
DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"], "support": ["read:catalog"]}'
```

- **A value is strict.** At most one `@`; the role and the client each match the id rule above. A malformed id or value fails boot, naming it and `AGENT_REGISTRATIONS`. `__` has no meaning anywhere: it is part of the name.
- **`"{role}@{client}"`** is that id's `clients` entry, so it needs an explicit `RootConfig(deployments_root=...)`. `"{role}"` alone means no deployment override.
- **Only predefined roles.** An environment variable cannot carry an `Agent`; a custom agent is registered with `agents=`.
- **`agents=` wins.** When `create_app` gets `agents=`, `AGENT_REGISTRATIONS` is ignored, never merged.

### Migrating from the old runtime ids

Before ADR-004, the Settings-driven boot read the role and the deployment out of the runtime id itself: `acme__sales-agent`, or `_generic__sales-agent` for no deployment. That scheme is gone. A runtime id is only a key, and a deployment still configured the old way fails boot, naming the unregistered id and pointing at `AGENT_REGISTRATIONS`. To migrate:

1. **Choose an id for each runtime.** Keeping the old string works only where it is a valid id: `acme__sales-agent` is one (now just a name), `_generic__sales-agent` is not (an id cannot start with `_`).
2. **Register it** in `AGENT_REGISTRATIONS`: what was `acme__sales-agent` becomes `"sales-agent@acme"`, what was `_generic__sales-agent` becomes `"sales-agent"`.
3. **Rename the id everywhere it is used**: `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID`, and every `DEPLOY_GRANTS` key. An old `DEPLOY_GRANTS` key is never matched to a renamed id: the runtime fails boot for lack of a grant.
4. **Update OpenAI-compatible clients** (e.g. Open WebUI): they send the id as `model`, and `/v1/models` now lists the new ids.

| Before | After |
|---|---|
| `ADAPTER_RUNTIMES='["_generic__sales-agent"]'` | `AGENT_REGISTRATIONS='{"sales": "sales-agent"}'` and `ADAPTER_RUNTIMES='["sales"]'` |
| `WHATSAPP_RUNTIME_ID=acme__sales-agent` | `AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme"}'` and `WHATSAPP_RUNTIME_ID=acme-sales` |
| `DEPLOY_GRANTS='{"acme__sales-agent": [...]}'` | `DEPLOY_GRANTS='{"acme-sales": [...]}'` |

`MODEL_PRICES` needs no change: it is keyed by the provider model id, never by a runtime id. `agents_system.integration.openai_adapter.to_model_id` and `parse_model_id` are deleted with the scheme, with no replacement: code that imports them fails at import time. Register through `create_app(agents=...)` or `AGENT_REGISTRATIONS` instead.

---

## Memory scoping in deployments

The memory layer follows the same two-level scope:

| Scope | What it stores |
|---|---|
| `platform / {role_type}` | Predefined agent behavior learned over time across all deployments |
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
