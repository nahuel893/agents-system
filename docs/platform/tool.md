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
| **T2** | Scoped write/send, or a query the model writes | order writer, `send:message`, `sql_query` | Always revalidated at call time |
| **T3** | Host execution | `use_term`, `read_file` | `operator-agent` branch only |

Two deterministic consequences follow from tier, both replacing (as a superset of, never a narrowing of) the older `write:`/`send:` prefix heuristic:

- **Interceptor Layer 2 (call time).** Any T2 or T3 tool is revalidated against current permissions at every call, regardless of how its `required_permissions` are named. `always_revalidate: true` extends the same revalidation to a specific T0/T1 tool without reclassifying it.
- **Capability Injector (build time), second barrier.** A role whose `policy.md` declares `untrusted_input: true` (ADR-002 C.11) never receives a T3 tool, even if the role's manifest and the requesting identity's grants would otherwise satisfy the tool's `required_permissions`. This is independent of C.11's `untrusted_input`/`exec:*` mutual-exclusion invariant: that invariant blocks by *permission family*, this barrier blocks by *tier*, so a T3 tool registered under a permission with no `exec:` prefix is still caught.

**Construction-time invariant.** Both of the above depend on `tier` actually reflecting a tool's danger — a tier is only as trustworthy as the author who set it. `ToolSpec` therefore validates itself at construction (`__post_init__`): any `required_permissions` entry in the `write:`/`send:` family (case-insensitive, whitespace-tolerant) requires `tier` to be `T2` or `T3`; any `exec:*` permission requires `tier=T3` specifically. A mismatch raises `ValueError` immediately, for every caller — production tool builders and test fixtures alike — so a tool author cannot silently under-tier a dangerous permission and have it slip past both enforcement layers above.

---

## Declarative command tools (`command_tools`)

ADR-002 C.12. The only way to expose a host command before this was the
single generic `use_term` connector, gated entirely by an allowlist of bare
program names (`TerminalPolicy.allowed_commands`) — a role got
unrestricted-within-allowlist access or none, and the allowlist said nothing
about the *argument shape* that makes an allowlisted command exploitable
(`git -c core.sshCommand=...`, `find . -exec rm {} \;`, `psql -c "DROP
TABLE..."`, `curl -d @/etc/secret` are all real examples of a
name-allowlisted command turned dangerous by its arguments).

`command_tools` closes that gap: a role's `manifest.md` may declare one or
more fixed-`argv` command tools, each with typed, pattern-validated
parameter placeholders. There is no free-form argv and no shell — only the
declared parameters vary, and only within the constraints the manifest
author wrote for them.

```yaml
command_tools:
  - name: check_stock
    argv: ["/usr/bin/inventory-cli", "--sku", "{sku}", "--format", "json"]
    params:
      sku:
        type: string
        pattern: "^[A-Za-z0-9_-]{1,32}$"
        max_length: 32
    tier: T2
    permission: run:check_stock
```

### Schema

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `string` | required | Unique identifier for this command tool within the role. |
| `argv` | `list[string]` | required | Fixed argv template. Each element is either a literal (author-controlled, never varies) or a `{param}` placeholder that must occupy the WHOLE element — `--flag={param}` is invalid. `argv[0]` is the program path; it may never itself be a placeholder. |
| `params` | `object` | optional | Typed, per-parameter validation. Each key is a param name referenced by a `{param}` placeholder in `argv`; each value declares `type` (`string` or `integer`), and — **required for every `string` param** — `pattern` (regex) or `enum` (list of allowed values); `max_length` (integer, string params, capped) stays optional. |
| `tier` | `T0 \| T1 \| T2 \| T3` | required | Capability tier (see "Capability tiers" above) — author-declared per entry, not fixed, but **enforced with a floor: `T2` or `T3` only.** `T0`/`T1` are rejected at load. `check_stock` above is `T2` — the minimum, and the tier ADR-002 C.12 deliberately allows an `untrusted_input` role to hold, precisely *because* the fixed `argv` plus a mandatory `pattern`/`enum` on every string param keep it narrow. Use `T3` for anything that mutates host state or can reach arbitrary paths (broader than a single narrowly-shaped read/action). |
| `permission` | `string` | required | Must start with `run:` — a permission family distinct from `exec:*`. This is what lets an `untrusted_input` role (ADR-002 C.11) safely hold a narrow command tool without tripping C.11's `untrusted_input`/`exec:*` mutual-exclusion invariant: the invariant blocks the `exec:*` family specifically, and a command tool is never in it. |

### The enforced tier floor

A `run:*` permission (every command tool's permission family) requires `tier` in `{T2, T3}` — enforced twice, independently: `ToolSpec.__post_init__` (`harness/registry.py`) raises `ValueError` at construction, and the loader (`harness/loader.py`) rejects a `T0`/`T1` command tool at manifest load with a `DefinitionError` naming the tool, so a misconfigured role never even resolves. The reason is mechanical, not stylistic: `interceptor._is_sensitive` only revalidates a tool at call time when its tier is `T2`/`T3` (or it opts in via `always_revalidate`) — a `T0`/`T1` command tool would be equipped for an `untrusted_input` role AND never revalidated, which is exactly the combination ADR-002 C.10's second barrier exists to prevent for `use_term`/`read_file` and must equally prevent here.

`T2` on an `untrusted_input` role stays allowed — that is ADR-002 C.12's whole point, and its own worked example (`check_stock` above) uses it. What makes a `T2` command tool safe for untrusted input is not the tier alone; it is the combination of a fixed `argv` template (no free-form arguments at all) and a mandatory `pattern`/`enum` on every string param (see "Narrowness is required" below). `T3` is for a command tool broader than that — one that mutates host state or can reach arbitrary paths.

### Narrowness is required, not opt-in

Every `string` param **must** declare `pattern` or `enum` — the loader rejects a `string` param with neither at load time. An unconstrained `type: string` param with no `pattern` is not a narrow command tool at all: it is a way to smuggle an arbitrary value into the command's argv, which is exactly what a `T2` tier on an `untrusted_input` role is supposed to rule out by construction. `max_length` stays optional, but when given it is capped (4096 characters) — a `max_length` an order of magnitude beyond what any real parameter needs is not a meaningful narrowing constraint either.

### Load-time safety rules

Enforced by the loader (`harness/loader.py`) before a role can even resolve, so a malformed declaration fails at deploy time, not at the first call:

- A placeholder must occupy a **whole argv element**. A partial-element placeholder (`--flag={x}`) is rejected, because that shape is exactly how option injection sneaks in — `--flag=--evil-flag` would otherwise smuggle a second flag through what looks like an ordinary value.
- Every placeholder in `argv` must reference a declared param, and every declared param must be used by at least one placeholder.
- `argv[0]` is resolved to an **absolute path once, at load time** — a bare name is looked up on `$PATH` here and only here; nothing re-resolves it against `$PATH` at call time, which removes the `$PATH`-manipulation vector.
- `permission` must be in the `run:*` family.
- `tier` must be `T2` or `T3` — see "The enforced tier floor" above.
- Every `string` param must declare `pattern` or `enum`, and any declared `max_length` may not exceed the platform cap (4096) — see "Narrowness is required" above.
- A deployment override may only **remove** command tools from what the role declares, mirroring the subtractive rule the rest of the manifest already follows (see `docs/platform/deployment.md`) — it can never add one the role did not declare, and it never redefines an entry's `argv`/`params`/`tier`.

### Call-time validation and execution

The connector built for a declared command tool (`connectors/command_tools.py`) rejects, before ever substituting a value into the template: an unknown parameter name, a missing required parameter, a value of the wrong declared type, a value that fails its `pattern`/`max_length`/`enum`, and — independent of all of the above — any value whose first character is `-` (U+002D) **or a Unicode dash lookalike** (en dash `–`, em dash `—`, any other Unicode category-Pd dash, or U+2212 MINUS SIGN specifically, since that one is category Sm and would otherwise slip past a category-only check). That guard is the direct closure of the option-injection class in the attack table above: none of `-c core.sshCommand=...`, `-exec rm`, `-c "DROP TABLE..."`, or `-d @/etc/secret` — nor a lookalike-dash variant of any of them — can ever reach a command tool's argv as a parameter value, regardless of what `pattern` a param author did or did not write.

**Consequence, by design:** a negative integer value (e.g. `-1`) can never be passed through a command tool param — there is no narrow way to distinguish "a negative number" from "an option flag" at this layer, so both are refused. A command tool that genuinely needs a signed value must accept it as a `string` with a `pattern` that spells out its own sign-handling (e.g. an explicit sign word, or a param that is never negative in practice).

Once every value validates, the connector substitutes them into the template and executes the result through the SAME no-shell subprocess engine `use_term` uses (`create_subprocess_exec`, never a shell; the same timeout and output-cap machinery) — a command tool is not a second command runner, it is a declarative front end over the one execution seam the platform already hardened. That single shared seam is also what lets ADR-002 C.14's bubblewrap sandbox wrap both `use_term` and every command tool identically — see "T3 sandbox (bubblewrap)" below.

### T3 sandbox (bubblewrap)

ADR-002 C.14. `TerminalPolicy.sandbox` is a required `SandboxPolicy` field — no default, the same posture `root` and `allowed_commands` already have. Every command run through the shared seam above (`use_term` and every `command_tools` entry) runs inside `bwrap`: no network unless the tool's policy explicitly declares it, the fixed system paths (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`) read-only, a private `/tmp`, `policy.root` read-write, a cleared environment, and a memory/CPU ceiling enforced via `RLIMIT_AS`/`RLIMIT_CPU` (bubblewrap itself has no resource-limit flags). If `bwrap` is not on the host, the command refuses rather than falling back to running unsandboxed.

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

### `sql_query`

| Field | Value |
|---|---|
| Connector | `connectors/sql_query_connector.py`, over a dedicated engine for the `sql_readonly` role (`scripts/provision_sql_readonly.sql`) |
| Required permissions | `query:sql` (the `Query` family) |
| Tier | `T2` — the model writes the query; see ADR-007 for the tier decision |
| Inputs | `sql` (string: one PostgreSQL `SELECT` or `WITH … SELECT` over the deployment's allowlisted views) |
| Outputs | `columns` (list), `rows` (list of lists, JSON-safe; money as strings), `row_count`, `truncated` (more rows matched than `row_limit`, or the byte budget cut them), `truncated_bytes` (the rows were cut at `byte_limit`), `row_limit`, `byte_limit`, `empty_result` (the query ran and matched nothing), `relations` (views read), and `note` (a fixed explanation, only when `truncated_bytes`) |
| Errors | Fixed texts with an `error_kind`: `query_rejected` (with a `reason` code), `query_timeout`, `refused_by_database`, `query_invalid`, `data_error`, `value_out_of_range` (a value the driver cannot represent in Python, such as a date past year 9999), `query_failed` (also any other failure), `database_unavailable`, `role_not_read_only`, `sql_not_configured`. None carries database or driver output, and no failure escapes as an exception. |

Runs one read-only query the model wrote, for questions the fixed `run_report` catalog cannot answer. This is the one tool that runs model-authored SQL, so the database role is its boundary, not the application: the role can read only the allowlisted views and write nothing, every call runs in a `READ ONLY` transaction with a server-side statement timeout, and the tool re-verifies the role on every call and refuses to run if it could write or read beyond the allowlist. An application guard on top parses the query and executes only its canonical rendering, capped at `row_limit + 1` rows and at `byte_limit` bytes (default 64 KiB): the database measures every row and never sends one larger than the budget, and the tool fetches through a cursor, 10 rows at a time, and stops once the budget is spent. Every view it reads must be a materialized view or a `security_barrier` view. PostgreSQL does not bound a query's memory, so run the tool's database where an oversized allocation fails instead of being OOM-killed (`vm.overcommit_memory = 2`), ideally a dedicated replica: on a host that OOM-kills, one query restarts the whole cluster. No predefined role equips it; a deployment registers it with `build_sql_query_tool_spec(engine, SqlQueryConfig(views=...))`. Provisioning, the threat model and the AD-2 amendment are in [ADR-007](../architecture/adr-007-read-only-sql-tool.md).

---

## Cross-references

- Permission model and connector-specific injection rules: `docs/architecture/permission-model.md`
- Read-only SQL tool, its trust boundary and the AD-2 amendment: `docs/architecture/adr-007-read-only-sql-tool.md`
- Injection pipeline and ordering: `docs/platform/harness.md`
- Skill definitions (behavioral counterpart to tools): `docs/platform/skill.md`
- Agent `manifest.md` `tools` field: `docs/platform/role.md`
