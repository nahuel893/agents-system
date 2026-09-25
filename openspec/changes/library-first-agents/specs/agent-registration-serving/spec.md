# Agent Registration and Serving Specification

## Purpose

Today `create_app`'s lifespan resolves runtimes by re-deriving a role name
from a `{deployment}__{role}` string (with a `_generic` sentinel for "no
deployment"), independently re-parsed in `main.py` and in
`integration/openai_adapter.py`'s `to_model_id`/`parse_model_id`. That
convention has no way to serve an importer-defined `Agent` — it only ever
resolves a name under `platform_root/roles`.

This capability replaces that implicit string convention with explicit
registration: a deployer supplies a `{id: Agent}` mapping (or an equivalent
explicit registration call) at `create_app` time, choosing each runtime id
themselves. The two duplicated string parsers are deleted, not
consolidated into a third copy. This spec does not restate the
`Permission`/`Tier` model or grant semantics — see
`openspec/specs/permission-hierarchy/spec.md`.

## Requirements

### Requirement: create_app accepts an explicit {id: Agent} registration

`create_app` MUST accept an explicit mapping from a deployer-chosen runtime
id (a string) to a registered `Agent`, in place of resolving role-name
strings against `platform_root/roles` inside the lifespan loop. Each entry
in the mapping is independently resolved into a runtime through the same
`resolve`/`merge`/`build_runtime` pipeline the `agent-definition-locator`
capability defines, regardless of whether the registered `Agent` wraps a
predefined-role locator, an importer-folder locator, or an inline locator.

#### Scenario: A predefined role is registered under a deployer-chosen id

- GIVEN `create_app(agents={"acme-sales": Agent.predefined("sales-agent", client="acme")})` (or the equivalent registration call)
- WHEN the application boots
- THEN a runtime is built for `"acme-sales"`, using the predefined-role locator branch, byte-for-byte equivalent to what `{deployment}__{role}` resolution produced for the same role and deployment before this change

#### Scenario: An importer-defined custom agent is registered and served

- GIVEN `create_app(agents={"triage-bot": my_custom_agent})` where `my_custom_agent` is built via `Agent.from_folder(...)` or `Agent(...)`
- WHEN the application boots
- THEN a runtime is built and cached for `"triage-bot"`, and it is servable through the same code paths (OpenAI adapter, WhatsApp binding) available to a predefined-role registration

### Requirement: Runtime ids are deployer-chosen, non-empty, and unique per registration

A runtime id MUST be a non-empty string. Within one `create_app`
registration mapping, every runtime id MUST be unique — the mapping's own
key uniqueness enforces this structurally. The system MUST NOT derive,
validate, or reject a runtime id based on any encoded-substring convention
(no `__` separator meaning, no `_generic` sentinel, no implied role name).

#### Scenario: An empty-string id is rejected

- GIVEN a registration mapping containing an entry with `""` as its id
- WHEN `create_app` processes the mapping
- THEN it MUST raise an error naming the empty id as invalid, before any runtime is built

#### Scenario: An arbitrary id string with no embedded convention is accepted

- GIVEN a registration mapping with the id `"acme-support-v2"` — a string with no `__` separator and no relationship to any role name
- WHEN `create_app` processes the mapping
- THEN the id is accepted as-is and used as the runtime's cache key and `/v1/models` id, with no parsing attempted on it

### Requirement: The {deployment}__{role} scheme and its sentinel are removed

The `{deployment}__{role}` runtime-id convention and the `_generic` sentinel
value (previously meaning "no deployment") MUST be removed. A runtime id
supplied to `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID`, or as a `DEPLOY_GRANTS`
key MUST be treated as an opaque string matching a key in the `{id: Agent}`
registration mapping — never split, parsed, or interpreted for embedded
role/deployment information.

#### Scenario: A legacy-shaped id string is treated as opaque, not parsed

- GIVEN `ADAPTER_RUNTIMES` (or the registration id) is set to the literal string `"acme__sales-agent"`
- WHEN it is matched against the `{id: Agent}` registration mapping
- THEN the system looks up `"acme__sales-agent"` as a single opaque key; it MUST NOT split it on `"__"` or interpret `"acme"`/`"sales-agent"` as a deployment/role pair

#### Scenario: A runtime id needs no _generic sentinel to mean "no deployment"

- GIVEN a registered `Agent` with no deployment override, registered under the id `"support-bot"`
- WHEN it is resolved
- THEN nothing about `"support-bot"` needs to equal or contain `"_generic"` to be treated as deployment-less — deployment-less-ness is a property of the registered `Agent`, not encoded in the id string

### Requirement: to_model_id/parse_model_id and the inline main.py parser are deleted

`to_model_id`/`parse_model_id` (`integration/openai_adapter.py`) and the
inline `model_id.split("__", 1)` parsing logic previously duplicated in
`main.py`'s lifespan loop MUST both be deleted in the same change, together.
No consolidated or renamed successor function that re-implements the same
string-splitting convention MAY exist anywhere in the codebase after this
change ships.

#### Scenario: No duplicated runtime-id parsing logic remains

- GIVEN the codebase after this change ships
- WHEN it is searched for any function that splits a runtime id on `"__"` to recover a deployment/role pair
- THEN no such function exists — runtime ids are opaque registration-mapping keys everywhere they are used

#### Scenario: Any external caller importing the deleted functions fails at import time

- GIVEN external code that does `from agents_system.integration.openai_adapter import to_model_id`
- WHEN that import executes after this change ships
- THEN it MUST raise `ImportError` — this is an intentional, documented breaking change (see Backward Compatibility & Migration)

### Requirement: /v1/models lists exactly the ids the operator named for the adapter

`GET /v1/models` MUST list exactly the runtime ids the operator explicitly
named for the OpenAI-adapter surface (the registration's adapter-exposed
subset), not every id registered in the `{id: Agent}` mapping. A runtime
registered only for another channel (e.g. WhatsApp) and not named for the
adapter MUST NOT appear in `/v1/models`, preserving the existing "publish
only what was named" behavior.

#### Scenario: An adapter-named id appears in /v1/models

- GIVEN a runtime id is included in the operator's adapter-exposed set
- WHEN `GET /v1/models` is called
- THEN that id appears in the response's `data` list, in the existing OpenAI models-list shape (`id`, `object: "model"`, `created`, `owned_by`)

#### Scenario: A WhatsApp-only runtime does not leak into /v1/models

- GIVEN a runtime id is registered and used only as the WhatsApp binding, and is not included in the operator's adapter-exposed set
- WHEN `GET /v1/models` is called
- THEN that id does NOT appear in the response's `data` list, even though a runtime for it exists in the process's runtime cache

### Requirement: WhatsApp runtime binding resolves by registered id

The WhatsApp channel's runtime binding MUST resolve by looking up the
operator-configured WhatsApp runtime id directly in the `{id: Agent}`
registration mapping. An operator-configured WhatsApp runtime id that has no
matching entry in the registration mapping MUST fail boot loudly, naming the
unmatched id.

#### Scenario: A registered id is correctly bound to WhatsApp

- GIVEN `WHATSAPP_RUNTIME_ID="support-bot"` and `"support-bot"` is a key in the `{id: Agent}` registration mapping
- WHEN the application boots
- THEN the WhatsApp webhook worker binds to the runtime built for `"support-bot"`

#### Scenario: An unmatched WhatsApp runtime id fails boot

- GIVEN `WHATSAPP_RUNTIME_ID="ghost-bot"` and no `"ghost-bot"` entry exists in the registration mapping
- WHEN the application attempts to boot
- THEN boot MUST fail with an error naming `"ghost-bot"` as unmatched, and the application MUST NOT start

### Requirement: WhatsApp binding still requires untrusted_input=true

The existing ADR-002 C.11/C.13 boot-time channel-safety check MUST continue
to apply, unchanged in substance, to whichever `Agent` is registered under
the WhatsApp runtime id: if its resolved definition has
`untrusted_input=False`, boot MUST fail with `DefinitionError` naming the
role (or agent name) and the runtime id, refusing to bind an
input-trusted-by-default agent to attacker-reachable external input.

#### Scenario: A trusted-input agent cannot be bound to WhatsApp

- GIVEN an `Agent` registered under the WhatsApp runtime id resolves `untrusted_input=False`
- WHEN the application attempts to boot
- THEN boot MUST fail with `DefinitionError` naming the agent and the WhatsApp runtime id, and the application MUST NOT start

#### Scenario: An untrusted-input agent binds successfully

- GIVEN an `Agent` registered under the WhatsApp runtime id resolves `untrusted_input=True`
- WHEN the application boots
- THEN the WhatsApp binding succeeds

### Requirement: WhatsApp binding still enforces the outbox lease timing budget

The existing check that a WhatsApp-bound runtime's total execution timeout
plus its non-turn headroom must remain below the outbox lease duration MUST
continue to apply unchanged, regardless of which locator kind produced the
WhatsApp-bound `Agent`.

#### Scenario: A WhatsApp-bound custom agent with too generous a timeout fails boot

- GIVEN an importer-defined `Agent` registered under the WhatsApp runtime id declares `total_execution_timeout_s` such that, plus the fixed non-turn headroom, it meets or exceeds the outbox lease duration
- WHEN the application attempts to boot
- THEN boot MUST fail with `ValueError` naming the runtime id and the computed timeout values

### Requirement: DEPLOY_GRANTS is keyed by the registered runtime id

`DEPLOY_GRANTS` MUST remain the sole grant source for every runtime
configured for boot, keyed by the deployer-chosen registration id (not by
any `{deployment}__{role}` string). A registered runtime id with no matching
`DEPLOY_GRANTS` entry MUST fail boot loudly, naming the runtime id and
(when the registered `Agent` wraps a predefined-role locator) the role name.
The dict shape (`dict[str, tuple[str, ...]]`) and the intersection/ceiling
semantics defined in `openspec/specs/permission-hierarchy/spec.md` are
unchanged.

#### Scenario: A registered id without a DEPLOY_GRANTS entry fails boot

- GIVEN a runtime is registered under the id `"support-bot"` and `DEPLOY_GRANTS` has no `"support-bot"` key
- WHEN the application attempts to boot
- THEN boot MUST fail with `DefinitionError` naming `"support-bot"` as missing its `DEPLOY_GRANTS` entry, and the application MUST NOT start

#### Scenario: An id migrated from the old key format resolves correctly

- GIVEN an operator previously configured `DEPLOY_GRANTS='{"acme__sales-agent": [...]}'` under the old scheme, and now registers the same agent under the new chosen id `"acme-sales"` with `DEPLOY_GRANTS='{"acme-sales": [...]}'`
- WHEN the application boots
- THEN the grant resolves correctly under the new key; the old key format is never consulted or matched

### Requirement: No implicit client-override root is assumed for a registered agent

When a registered `Agent` requires a deployment override (a predefined role
combined with a client), and no `RootConfig(deployments_root=...)` was
supplied to `create_app`, boot MUST fail loudly with `DefinitionError`
naming the runtime id and stating that an explicit `RootConfig` is required
— the package derives no default `deployments_root` from its own
installation location. This preserves existing behavior; it is not new.

#### Scenario: A client-override registration with no RootConfig fails boot

- GIVEN a registered `Agent` wraps a predefined role with a client override, and `create_app` was called with `roots=None`
- WHEN the application attempts to boot
- THEN boot MUST fail with `DefinitionError` naming the runtime id and requiring an explicit `RootConfig(deployments_root=...)`

#### Scenario: An importer-folder or inline agent needs no deployments_root of its own

- GIVEN a registered `Agent` built via `Agent.from_folder(...)` with no client-override component
- WHEN `create_app` is called with `roots=None`
- THEN boot does not fail on this account — an importer-defined agent's own folder is not a client-override deployment and does not require `deployments_root`

### Requirement: No default deployments path ships inside the installable package

The package MUST ship no default `deployments_root` path and no
client-specific example data inside the installable `agents_system` module.
The only application entrypoint the package previously shipped inside the
importable module (`agents_system.demo`) MUST be relocated to a
non-installed `examples/` location; `python -m agents_system.demo` MUST NOT
be available after this change ships.

#### Scenario: The installed package contains no application entrypoint

- GIVEN the package installed from a built distribution
- WHEN `python -m agents_system.demo` is attempted
- THEN it MUST fail (module not found) — no such module ships inside the installed package

#### Scenario: An equivalent example entrypoint exists outside the package

- GIVEN the repository after this change ships
- WHEN a developer looks for a runnable example of `create_app` usage
- THEN an equivalent script exists under `examples/`, documented by an updated `docs/platform/demo-entrypoint.md`

### Requirement: Boot fails loudly for every unresolvable registration, before serving begins

Every boot-time failure introduced or preserved by this capability — an
unresolvable `Agent` locator, a missing `DEPLOY_GRANTS` entry, an unmatched
`WHATSAPP_RUNTIME_ID`, an `untrusted_input` mismatch on the WhatsApp
binding, a lease-timing violation, or a missing `deployments_root` for a
client-override registration — MUST prevent the application from starting.
None of these failure modes MAY be silently downgraded to "serve with a
degraded or partial runtime set."

#### Scenario: One bad registration entry blocks the whole boot

- GIVEN a registration mapping with two valid entries and one entry that fails resolution (e.g. an unresolvable `extends:`)
- WHEN the application attempts to boot
- THEN boot MUST fail entirely — the two valid entries MUST NOT be served while the failing one is silently dropped
