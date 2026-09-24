# Explore: permission-model

**Change:** `permission-model`  
**Package:** `agents_system`  
**Dependency position:** This change must precede `library-first-agents`; the latter should consume this public permission API rather than define one.  
**Skill resolution:** `none` — no phase-skill path was injected.  
**Scope:** analysis only; no source code was changed.

## Decision context

The owner-approved direction is a class hierarchy rooted at `Permission`, with built-ins `Read`, `Send`, `Write`, `Exec`, and `Run`; string values remain the manifest/YAML wire format. The hierarchy is open to downstream subclasses and wholly new actions. Safety must derive from declared `Tier`, not action names.

## Current model and call path

1. `Tier` is a `str, Enum` with T0–T3 at `src/agents_system/harness/registry.py:9-33`; `ToolSpec.required_permissions` is `tuple[str, ...]` at `:48-51`.
2. `ToolSpec.__post_init__` still treats string prefixes as safety metadata: `exec:` requires T3 (`registry.py:83-89`), `write:`/`send:` require T2/T3 (`:90-101`), and `run:` requires T2/T3 (`:102-117`). This is the prefix logic that R2 replaces.
3. `AgentDefinition.permissions` and `RawDefinition.permissions` are string tuples/lists at `src/agents_system/harness/loader.py:207` and `:274`. Generic role-chain composition unions plain child string lists at `loader.py:1010-1031`; deployment merging resolves directives and rejects a resolved string not in the parent role at `:1423-1467` and `:1692-1703`.
4. The factory resolves the definition, then gives the same supplied iterable to registry and declarative-command surfaces at `src/agents_system/harness/factory.py:206-257`. Both injector paths calculate `effective = set(definition.permissions) & set(granted_permissions)` at `src/agents_system/harness/injector.py:102-169` and `:172-224`.
5. A model receives only granted `ToolSpec`s. At execution, `_execute_tools` forwards the turn permissions to `intercept` (`src/agents_system/agent/graph.py:120-177`); `intercept` revalidates only T2/T3 or `always_revalidate` tools (`src/agents_system/harness/interceptor.py:35-45`, `:112-153`).

### Layer-2 gap (#38)

The injector uses the deploy/build grant intersection, but `intercept` checks only `current_permissions` (`interceptor.py:124-145`), with no retained deploy grant ceiling. `AgentRuntime.run_turn` defaults that value to `self.permissions`, i.e. the role definition's full permission tuple (`src/agents_system/agent/graph.py:395-398`, `:415-476`). In production startup, `main.py` explicitly passes that same full role tuple as `granted_permissions` (`src/agents_system/main.py:299-360`). Therefore Layer 2 cannot distinguish a narrower deployment grant from the role ceiling; #38 correctly identifies the missing grant-aware revalidation boundary.

## Permission-string inventory

### Production package

| Area | Current string use | Evidence |
|---|---|---|
| Public library example | Shows `required_permissions=("read:catalog",)` and `granted_permissions=["read:catalog"]`. | `src/agents_system/__init__.py:15-37` |
| Loader/manifests | Parses list/directive strings; command declarations require `permission` starting with `run:`. | `loader.py:274`, `:560-639`, `:1010-1031`, `:1423-1467`, `:1692-1703` |
| Tier constraints | Prefix-based validation of `exec:`, `write:`, `send:`, and `run:`. | `registry.py:36-117` |
| Injection | String-set intersection and subset tests for registry and command tool surfaces. | `injector.py:77-99`, `:102-169`, `:172-224` |
| Interception | `current_permissions` is an iterable of strings and required string subset is rechecked for sensitive tools. | `interceptor.py:69-153` |
| Runtime state | `current_permissions` is carried into graph state and passed to Layer 2. | `agent/graph.py:120-177`, `:415-476`; `agent/state.py:12-14` |
| Command tools | `CommandToolDeclaration.permission` is a string; built specs use it unchanged. | `loader.py:223-260`, `:621-634`; `src/agents_system/connectors/command_tools.py:237-260` |
| Operator tools | `use_term` needs `exec:command`; `read_file` needs `read:files`. | `src/agents_system/connectors/operator.py:818-842` |
| Order/report tools | `order_writer` requires `write:orders` plus `write:order_items`; `run_report` requires `read:reports`. | `src/agents_system/connectors/order_connector.py:202-204`; `src/agents_system/connectors/report_connector.py:185-196` |
| Generic platform tools | Knowledge, summarizer, and escalation specs require `read:knowledge_base`, `read:conversation_logs`, and `send:escalation`. | `src/agents_system/connectors/platform_connectors.py:221-223`, `:315-317`, `:422-424` |
| Reference backends | Catalog, client lookup, and messaging specs use `read:catalog`, `read:client_registry`, and `send:message`. | `src/agents_system/services/reference.py:337-339`, `:361-363`, `:415-417` |
| Application boot | Resolves first, then grants the definition's entire role permission tuple; this is AD-5 and must be removed. | `src/agents_system/main.py:299-360`; regression test `tests/test_main.py:1-137` |
| OpenAI adapter | Supplies no per-caller permissions, intentionally defaulting to the runtime role tuple. | `src/agents_system/integration/openai_adapter.py:222-264` |
| Evals | Scenario YAML accepts `granted_permissions` strings; absent means grant all resolved role permissions. Runner follows that default before factory construction. | `src/agents_system/evals/schema.py:56-65`, `:148-168`; `src/agents_system/evals/runner.py:347-383` |
| Live eval registry | Its `session_state` tool is T0 with no permissions; other tool builders carry their regular strings. | `src/agents_system/evals/live_registry.py:127-143`, `:170-201` |

### Shipped manifests and deployments

All shipped manifest permissions are YAML-frontmatter strings:

- `platform/roles/base/manifest.md:1-13`: `read:session`.
- `platform/roles/agent/manifest.md:1-13`: `read:session`, `send:escalation`.
- `platform/roles/sales-agent/manifest.md:1-17`: read catalog/client/price strings, `write:orders`, `write:order_items`, and `send:message`.
- `platform/roles/support-agent/manifest.md:1-15`: knowledge/conversation/client reads and `send:message`.
- `platform/roles/data-agent/manifest.md:1-16`: catalog/client/knowledge/report/session reads.
- `platform/roles/accountant-agent/manifest.md:1-14`: report and knowledge reads.
- `platform/roles/operator-agent/manifest.md:1-12`: `exec:command`, `read:files`.
- `platform/roles/developer-agent/manifest.md:1-12`: knowledge read, plus inherited operator permissions.
- `platform/roles/orchestrator/manifest.md:1-18`: client/session reads, `write:session`, three `spawn:*`, and `send:escalation`.
- `platform/roles/summary-agent/manifest.md:1-15`: conversation/knowledge/session reads and `write:summary_output`.

No shipped `command_tools:` declaration was found under `platform/`; its `run:<name>` strings currently occur in the loader, command-tools implementation, and command-tool tests. `deployments/` currently contains only `README.md`, so there is no deployed override manifest to migrate. The loader nevertheless supports deployment string list merging and validations (`loader.py:381-454`, `:1423-1467`, `:1655-1801`).

### Tests that encode the old representation

The strongest direct contracts are `tests/test_harness_injector.py:1-325` (intersection, missing strings, and T3 denial), `tests/test_harness_interceptor.py:116-380` (current-string revalidation), `tests/test_capability_tiers.py:1-380` (prefix/tier invariants), `tests/test_command_tools_injector.py:1-209` (run grants), `tests/test_untrusted_input_invariant.py:1-400` (exec-prefix behavior), `tests/test_harness_loader.py:92-259` and `tests/test_role_inheritance.py:62-75,462-464` (merged strings), `tests/test_platform_tools_integration.py:42-261`, `tests/test_platform_registries.py:103-151`, `tests/test_eval_schema.py:49-103`, `tests/test_eval_runner.py:398-438`, and `tests/test_openai_adapter.py:369`.

## Mapping R1–R4 onto existing enforcement

| Required rule | Current equivalent | Required replacement/retention |
|---|---|---|
| R1: subclass tier cannot be lower than parent | None; `Tier` belongs only to `ToolSpec`. | `Permission.__init_subclass__` must require a tier declared on each non-base subclass and reject a lower ordinal tier at class creation. |
| R2: tool/permission tier compatibility | Prefix branches in `ToolSpec.__post_init__`. | Resolve each required wire name through the registry, then compare registered class tiers rather than prefixes. Preserve fail-fast construction and tests. |
| R3: parent grant covers permitted descendants only | Exact string-set equality/subset only; no hierarchy. | A required class is satisfied by a granted ancestor only when `issubclass(required, granted)` and the required tier is not above the grant class tier; otherwise require an explicit grant. |
| R4: untrusted agent holds no T3 permission | Loader rejects `untrusted_input` + `exec:*` via string prefix; injector independently denies T3 tools. | Resolve role permissions to classes during loader validation and reject every T3 class for untrusted input. Retain the injector's T3-tool barrier as defence in depth. Reject or ignore a supplied T3 grant that cannot be part of an untrusted role's effective permission set; rejection is clearer. |

The existing C.11 path is `_is_exec_permission` (`loader.py:1498-1510`) plus `_validate_untrusted_input_exec` (`:1560-1585`) in both merged and unmerged resolve paths (`:1699-1703`, `:1852-1860`). It is intentionally case/whitespace tolerant and has substantial coverage in `tests/test_untrusted_input_invariant.py:101-400`. R4 generalizes this exact safety purpose from the `exec:` spelling to every T3 permission. The C.10 injector barrier already denies any T3 tool for untrusted input before permission comparison (`injector.py:77-99`) and is covered by `tests/test_harness_injector.py:151-325`.

**R2 wording check for proposal:** With T0 < T1 < T2 < T3, current compatibility is `tool.tier >= implied_permission_floor` (for example `write:` T2 can be used by a T2 or T3 tool; `exec:` requires T3). The literal stated R2 phrase, “cannot require a permission whose tier is lower than the tool,” expresses the opposite relation and would reject current-valid `Write` on T3. The proposal/spec must make the inequality explicit while preserving the owner-approved intended mapping; this is a wording clarification, not a reconsideration of the hierarchy decision.

## Migration choices

### A. Keep strings as the canonical wire format; resolve to classes at policy boundaries — recommended

- Manifests, YAML eval scenarios, existing public `ToolSpec` declarations, logging, and serialized audit data keep stable names such as `read:catalog`.
- A `PermissionRegistry` performs `name -> type[Permission]` and `type -> name`; loader validation, `ToolSpec` construction, grant normalization, injection, and Layer 2 operate on classes after conversion.
- Public APIs accept `type[Permission]` and optionally string names for `grant`; normalize once, reject unknown names immediately, and store immutable normalized class grants in `EquippedRuntime`.
- `AgentDefinition` may retain wire names for faithful manifest projection, while adding an internal resolved-permission view; this minimizes change to loader/eval serialisation and existing API users.

This is compatible with `agents_system` being a reusable library: its current public docs deliberately show external consumers registering a `ToolSpec` and passing string grants (`src/agents_system/__init__.py:15-37`), and `RootConfig` explicitly supports an external deployments tree (`loader.py:58-130`).

### B. Classes everywhere after parsing

Convert manifest names at `load_generic`/`load_override`, change `AgentDefinition.permissions`, `ToolSpec.required_permissions`, `Scenario.granted_permissions`, graph state, APIs, and tests to classes, then serialize only at boundaries. This gives stronger internal typing, but expands the blast radius and makes downstream manifest/audit/logging compatibility harder. It also cannot remove the registry because parsing still needs names. It is not recommended for the prerequisite change.

## Registry options

1. **Global process registry — recommended.** A package-owned singleton supports a single interpretation of a manifest name across library roles, consumer deployment manifests, `ToolSpec`, evals, and runtime grants. Built-ins register in the lightweight permission module. Third-party packages explicitly import/register before `resolve()`; collision on either name or class fails with both origins in the error. This fits the current global class-based tool API while preserving per-application `ToolRegistry` for connectors (`registry.py:131-161`, `main.py:261-262`).
2. **Per-app registry.** This would allow two apps in one process to map a name differently, but `RootConfig`/loader resolution would need a registry parameter everywhere, including tests and evals. A manifest could resolve differently by caller, weakening portability and making cached role definitions ambiguous.
3. **Entry-point/plugin discovery.** Useful later only as an opt-in discovery mechanism layered over explicit registration. It must be deterministic, have no import-time broad scan, and retain fail-closed collisions/unknown names; automatic third-party imports are an unnecessary boot-time and supply-chain surface for this change.

Registration rules should canonicalize only approved names, reserve built-in names, reject duplicate name-to-different-class and class-to-different-name mappings, and make name lookup fail loudly. A downstream permission action must be able to subclass `Read`/etc. or subclass `Permission` directly, declare its own tier, and register a unique wire name.

## Risks and mitigations

- **Ambiguous R2 inequality:** write the ordinal predicate and acceptance examples in the spec before implementation.
- **Import-order failures for extensions:** require registration before manifest resolution; include package/origin in unknown/collision errors.
- **String/class split-brain:** normalize at all ingress points and keep one canonical registry; never compare raw strings after conversion.
- **Layer-2 widening (#38):** persist the deployment grant ceiling in `EquippedRuntime` and revalidate against `current principal grant ∩ deploy grant ∩ role allowance`, not role permissions alone.
- **Cached runtime/per-principal overlap:** explicit deploy grants are a static ceiling; per-principal permissions remain a call-time input. ADR-002 A.2's missing identity path is still separate and should not be claimed solved by this change (`AgentRuntime.run_turn` already has a permission parameter at `agent/graph.py:415-476`; OpenAI currently omits it at `integration/openai_adapter.py:222-264`).
- **Untrusted input regression:** replace the `exec:`-prefix invariant only after R4 class validation and retain the T3 injector barrier.
- **Review size:** conversions touch core registry/loader/factory/injector/interceptor/runtime, public exports, main, evals, manifests, and extensive tests. This is likely beyond the 400-line review budget and should trigger `ask-on-risk` in planning rather than invent a chain strategy.

## Recommended approach

Implement the string-wire/class-policy hybrid with a package-global, explicit permission registry. Add built-in action classes and manifest-name leaves for all currently shipped strings; enforce R1 at subclass creation; replace prefix logic with a documented class-tier predicate; validate resolved manifest permissions and deploy grants loudly; preserve a normalized static grant ceiling in the equipped runtime; and make Layer 2 use that ceiling together with current principal grants. Keep strings in manifests, YAML scenarios, APIs where compatibility requires them, logs, and audit payloads. Update all current prefix and grant tests into class/tier equivalence tests, retaining dedicated tests for unknown names, collisions, R1, R3 inheritance at same and escalated tiers, R4 under arbitrary names, and the #38 Layer-2 regression.

No source implementation is proposed in this phase.
