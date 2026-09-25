# Proposal: Library-first custom agents (ADR-004)

## Intent

`agents_system` can today only *serve* the 8 packaged roles (optionally narrowed by a client deployment override). Every resolution path — `_role_folder`, `_load_role_files`, `_extends_target`, `load_generic`, `resolve()` in `harness/loader.py`, and the runtime-id scheme in `main.py`/`integration/openai_adapter.py` — hard-codes the assumption "a role is a name under `platform_root/roles`." There is no way for an importer to hand the library an agent it defines itself (from its own folder and/or from Python parameters), and no way to register it for serving. This blocks the stated product direction: the library ships the harness, tooling system, in-memory runtime, adapters, connectors, audit, and evals; **the importer defines arbitrary agents** on top of it.

Now is the right time because permission-model (ADR-003, merged, released as v0.2.0) just landed the safety layer every importer-defined agent must inherit for free: the `Permission`/`Tier` hierarchy, `DEPLOY_GRANTS`-based explicit deploy-time grants (no auto-grant), and persisted Layer-2 grant-ceiling revalidation (issue #38). Building the Agent API now means it is built directly on top of a tested, released safety foundation instead of racing it.

**Note on ground truth**: this proposal verified current source directly in this worktree (checked out from `origin/main`, which includes the merged permission-model change) rather than trusting `exploration.md` line numbers or the the main checkout's CodeGraph index verbatim — see "Ground-truth corrections" under Risks. All file:line references below are current as of this worktree.

Success looks like: an importer can define a custom `Agent` (folder and/or Python params), have it extend either the generic agent or a predefined role (additive inheritance), register it under a runtime id of its own choosing, and get every library invariant (ADR-002 B.8/B.9 base contract, C.10 tiers, C.11 `untrusted_input`⊥`exec:*`, T3 sandbox, ADR-003 permission rules) automatically — with zero behavior change for the 8 predefined roles or existing deployment overrides.

## Scope

### In Scope
- Rename "generic" → "predefined" for the 8 packaged roles, in code identifiers/comments and docs, wherever "generic" currently means those 8 roles (not the abstract `base`/`agent` tree itself, which stays the generic agent).
- `Agent` Python API: `Agent.from_folder(path, ...)` (reads `role.md`/`manifest.md`/`policy.md`, same three-file contract as today) and `Agent(name=..., extends=..., tools=..., permissions=..., skills=..., ...)` (pure Python params) — both producing the same `AgentDefinition` shape the loader produces today, and composable (folder + params together).
- Locator generalization (Approach 1 from exploration): a small discriminated locator — platform-role name (today's behavior, unchanged) | importer folder path | inline Python-built definition — threaded through `_role_folder`/`_load_role_files`/`_resolve_role_chain`/`load_generic`/`resolve()`, reusing the existing `merge`/injector/contract-invariant pipeline unchanged.
- Fix `_extends_target`'s silent last-segment stripping (`harness/loader.py:869-876`): an `extends:` value that cannot be placed in one of the two allowed locator spaces (platform-role tree, or the importer's own root) must raise `DefinitionError`, never silently re-resolve to a same-named platform role.
- Option B additive inheritance: a custom agent may extend the generic agent (`platform/roles/agent`) or any predefined role. Library invariants apply to every agent shape; deployment overrides stay subtractive-only and untouched.
- Skills sourced from an importer Agent's own folder `skills/` and/or inline Python-supplied content, in addition to (not instead of) the existing deployment-only skills mechanism for predefined roles. Resolve the `docs/platform/deployment.md:91-93` vs `docs/platform/role.md:7` contradiction as part of this.
- Runtime registration redesign: the deployer chooses a runtime id per agent at registration time; `create_app` receives a `{id: Agent}` mapping. The `{deployment}__{role}` / `_generic` sentinel scheme is removed. Both duplicated parsers (`integration/openai_adapter.py:54-74` `to_model_id`/`parse_model_id`, and `main.py`'s inline `model_id.split("__", 1)` at `main.py:280-281`) are deleted, not consolidated into a third copy.
- Migration story for `ADAPTER_RUNTIMES`, `WHATSAPP_RUNTIME_ID`, `DEPLOY_GRANTS` keys — breaking, documented (see Backward Compatibility & Migration).
- Guard 1: any change to a predefined agent's tools or permissions is a MAJOR (breaking) change recorded in `CHANGELOG.md`, enforced by tooling — a reviewed versioned snapshot of predefined agents' tools/permissions (extending `tests/platform_role_contract.py`'s existing `EXPECTED_ROLE_TOOLS` "deliberate, reviewed, independent expectation" pattern) plus a contract test/CI check requiring a breaking-change conventional-commit marker and a role `version:` bump when the snapshot diverges.
- Move `src/agents_system/demo.py` (and `docs/platform/demo-entrypoint.md`'s instructions) to `examples/` — this is the one place the package currently ships an application entrypoint inside the importable `agents_system` module. `RootConfig.deployments_root` (or an equivalent import-time parameter) becomes the only way to point at real deployments; the package ships no default deployments path and no client-specific data.
- `docs/architecture/adr-004-library-first-agents.md`, amending ADR-002 sections A.1, D, C.15, E.18, F with pointers (not rewrites).

### Out of Scope
- Per-principal identity (issue #15, ADR-002 A.2) — grants stay role/deploy-scoped.
- ADR-005 operational-safety roadmap items — referenced as context, not re-planned here.
- Guard 2 (explicit deploy-time grants, no auto-grant) — **already delivered** by permission-model/ADR-003 (`DEPLOY_GRANTS`, `EquippedRuntime` grant ceiling, Layer-2 revalidation, issue #38 fixed, issue #47 eval explicit grants). Referenced as a completed dependency, not re-implemented.
- `deployments/` top-level mount point and `tests/fixtures/agents/overrides/deployments/client-a/...` — **already correctly placed** (see Affected Areas: no action needed here; see Risks for why the briefing implied otherwise).
- Multiple inheritance or a full `RoleSource`-protocol rewrite (exploration's Approach 3) — rejected as over-engineered relative to the decided scope.
- Entry-point/plugin discovery for predefined roles.
- Any change to `load_override`'s subtractive-only deployment-override semantics for predefined roles.

## Capabilities

### New Capabilities
- `agent-definition-locator`: the generalized role/agent locator (platform-role name | importer folder | inline Python spec), the `Agent.from_folder`/`Agent(...)` API producing `AgentDefinition`, the `extends:` fail-loud fix, and skills resolution for importer agents.
- `agent-registration-serving`: deployer-chosen runtime ids, `{id: Agent}` registration into `create_app`, replacing `{deployment}__{role}`/`_generic` and the two duplicated parsers.
- `predefined-agent-governance`: the versioned tools/permissions snapshot, the SemVer-bump + CHANGELOG-entry contract check (Guard 1), and its release-please mapping.

### Modified Capabilities
- None. `openspec/specs/` is currently empty — no top-level capability specs exist to delta against. Note: `permission-hierarchy` (ADR-003's capability) lives only at `openspec/changes/archive/2026-09-25-permission-model/specs/permission-hierarchy/spec.md`; it was never promoted to `openspec/specs/`. This proposal's new capabilities should reference that spec by its actual archived path, and sdd-spec should flag the missing promotion step as a separate housekeeping item rather than block on it.

## Approach

Approach 1 from exploration (locator generalization), unchanged: widen `resolve()`'s notion of "what a role is" into a small locator instead of adding a parallel bypass module. Concretely —

1. **Locator core.** `_role_folder`/`_load_role_files`/`_resolve_role_chain`/`load_generic`/`resolve()` accept a locator discriminated union instead of an implicit "name under `platform_root/roles`" string. The platform-role branch is today's behavior, byte-for-byte. The importer-folder branch reads `role.md`/`manifest.md`/`policy.md` from a caller-supplied directory the same way `_load_role_files` does today. The inline branch accepts an already-built `RawDefinition`/spec and skips disk entirely. `extends:` resolution searches the same locator space and raises `DefinitionError` (never silently strips) when a value cannot be placed in it. `merge()`, the injector (`resolve_tool_surface`/`resolve_command_tool_surface`), `_append_base_contract`, and `_validate_untrusted_input_exec` are untouched — they already operate on the resolved `AgentDefinition`/`RawDefinition` shape, agent-shape-agnostic, which is exactly why every library invariant applies "for free."
2. **`Agent` API.** A thin builder (`Agent.from_folder(path, **skill_overrides)`, `Agent(name=..., extends=..., ...)`) that folds folder content and Python params into one `RawDefinition`-shaped object and calls the same `resolve`/`merge`/`build_runtime` primitives already proven for predefined roles. Exported from `agents_system/__init__.py`'s `_EXPORTS` dict alongside the existing lazy-import surface.
3. **Skills.** `_load_skills`'s source resolution (`harness/factory.py:124-166`) gains an importer-folder-and/or-inline-content branch; its `client is None` + declared-skills `FactoryError` stays exactly as-is for the deployment-override path (predefined roles still require a deployment to resolve skills, unchanged).
4. **Registration.** `create_app` accepts a `{id: Agent}` mapping (or an equivalent explicit registration call) instead of resolving role-name strings against `platform_root/roles` inside the lifespan loop (`main.py:250-374`). `to_model_id`/`parse_model_id` (`integration/openai_adapter.py:54-74`) and the inline `model_id.split("__", 1)` (`main.py:280-281`) are deleted together, in the same PR, to avoid a half-migrated state — this mirrors exploration's own risk note about partial renames drifting (ADR-002 F.19-21 precedent).
5. **Guard 1.** Extend `tests/platform_role_contract.py`'s existing `EXPECTED_ROLE_TOOLS` literal into a versioned, checked-in snapshot of every predefined role's `tools`/`permissions`; a new contract test fails when the resolved surface diverges from the snapshot unless (a) the role's `version:` frontmatter took a MAJOR bump and (b) `CHANGELOG.md` gained an entry for it. sdd-design settles the exact "what counts as released" reference (git tag vs. snapshot fixture) per exploration's Q6.

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `src/agents_system/harness/loader.py:700-701` (`_role_folder`) | Modified | Becomes locator-aware; platform-role branch unchanged |
| `src/agents_system/harness/loader.py:869-876` (`_extends_target`) | Modified | Fail loudly outside the two allowed locator spaces, no silent last-segment stripping |
| `src/agents_system/harness/loader.py:913+` (`_load_role_files`), `:1135+` (`_resolve_role_chain`), `:1185+` (`load_generic`), `:1843-1906+` (`resolve`) | Modified | Locator threading — the structural core of the change |
| `src/agents_system/harness/loader.py:1251+` (`load_override`), `:1653+` (`merge`) | Unchanged | Deployment-override subtractive semantics kept intact for backward compatibility |
| `src/agents_system/agent_spec.py` or `src/agents_system/agent.py` (new) | New | `Agent.from_folder(...)` / `Agent(...)` builder producing `RawDefinition` |
| `src/agents_system/harness/factory.py:124-166` (`_load_skills`), `:290+` (`build_runtime`) | Modified | Importer-folder-and/or-inline skills source, additive to existing deployment path |
| `src/agents_system/main.py:90` (`lifespan`), `:250-374` (runtime loop) | Modified | Replace `{deployment}__{role}` string loop with `{id: Agent}` registration; remove inline parser at `:280-281` |
| `src/agents_system/integration/openai_adapter.py:54-74` (`to_model_id`/`parse_model_id`) | Removed | Deleted, not consolidated — the new registration scheme has no equivalent string convention to parse |
| `src/agents_system/__init__.py:66-80` (`_EXPORTS`) | Modified | Add `Agent` (and any new locator/registration types) to the public lazy-export surface |
| `tests/platform_role_contract.py` | Modified | Gains the versioned tools/permissions snapshot + Guard 1 contract test; discovery stays scoped to `platform_root/roles` only |
| `src/agents_system/demo.py`, `docs/platform/demo-entrypoint.md` | Moved | Into `examples/` — the one current in-package application entrypoint |
| `tests/fixtures/agents/overrides/deployments/client-a/...` | No action | Already correctly placed under `tests/fixtures/` — see Risks |
| `deployments/README.md`, top-level `deployments/` | No action | Already ships empty as a mount point only — see Risks |
| `docs/platform/deployment.md:91-93`, `docs/platform/role.md:7` | Modified | Resolve the skills-location contradiction |
| `docs/architecture/adr-002-agent-model-and-capabilities.md` (A.1 `:94`, D `:1208`, C.15 `:1131`, E.18 `:1360`, F `:1429`) | Modified | Pointer/amendment additions only, per section |
| `docs/architecture/adr-004-library-first-agents.md` (new) | New | This change's ADR |
| `CHANGELOG.md`, `release-please-config.json` | Modified | Breaking-change entries per PR; `bump-minor-pre-major: true` already configured |

## Backward Compatibility & Migration

**This is a breaking release.** Every break, explicitly:

1. **Runtime id scheme removed.** `{deployment}__{role}` and the `_generic` sentinel are gone. A deployer now chooses an arbitrary id string per registered `Agent`. `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID` env vars change meaning: they hold deployer-chosen ids directly, with no embedded role/deployment encoding to parse.
2. **`DEPLOY_GRANTS` keys must be rewritten.** Existing keys of the form `"{deployment}__{role}": [...]` (e.g. `"acme__sales-agent"`) must become `"{chosen-id}": [...]` matching the new registration id. The dict shape (`dict[str, tuple[str, ...]]`) and "sole grant source, boot fails loudly without an entry" behavior from permission-model are unchanged.
3. **`to_model_id`/`parse_model_id` deleted**, not deprecated. Any external caller importing `agents_system.integration.openai_adapter.to_model_id`/`parse_model_id` breaks at import time.
4. **`create_app`'s implicit string-resolution loop is gone.** A caller previously relying on "point `ADAPTER_RUNTIMES` at a role name under `platform_root/roles` and it just resolves" must now register an explicit `Agent` (built via the predefined-role locator branch for unchanged behavior, or via the new importer-folder/inline branches for a custom agent).
5. **Terminology rename** (generic → predefined) touches identifiers/comments/docs. Flagged low external-impact per exploration — no current code or doc treats "generic" as a distinguishing public API name (it is used loosely for "platform-owned").
6. **`agents_system.demo` module path removed.** `python -m agents_system.demo` stops working; replaced by an `examples/` script. `docs/platform/demo-entrypoint.md` updated accordingly.
7. **`extends:` now fails loudly** where it used to silently collapse to a same-named platform role via last-segment stripping. A deliberate safety-positive break — no known manifest currently relies on the old silent behavior.
8. **Additive, not breaking:** skills resolution for importer agents (their own folder and/or inline content) is new and opt-in; the existing deployment-only skills path for predefined roles is unchanged.

Every break above ships with an explicit `BREAKING CHANGE:` conventional-commit footer so release-please's `bump-minor-pre-major: true` config (already in `release-please-config.json`) bumps the package's MINOR version while it stays 0.x — the same mechanism the permission-model release already used (v0.1.0 → v0.2.0). This is a separate space from Guard 1's per-role `version:` frontmatter, which is bumped MAJOR independently whenever a predefined role's own tools/permissions change, regardless of the package's own SemVer state.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| the main checkout's CodeGraph index is stale (missing the merged permission-model commits — no `CHANGELOG.md`, no `DEPLOY_GRANTS`, still shows the removed AD-5 auto-grant) | Confirmed, not hypothetical | This proposal used direct file reads against the worktree (checked out from `origin/main`) instead of that index; sdd-design and sdd-apply must do the same, or re-init CodeGraph against a current checkout, before trusting `codegraph_explore` output for this change |
| `exploration.md` (obs 643) describes `main.py`'s grant wiring as the pre-ADR-003 auto-grant (`granted_permissions=definition.permissions`) — this is now stale; the worktree's actual `main.py:344-374` already uses `settings.deploy_grants[model_id]` with a fail-loud check | Confirmed | Guard 2 treated as done per the briefing (correct); this proposal's Q4/runtime-id design accounts for the *current* `DEPLOY_GRANTS`-keyed shape, not the state `exploration.md` describes |
| Briefing states client-a "moves to tests/fixtures" and implies `deployments/` needs cleanup; both are already true today (`deployments/README.md` documents an intentionally empty mount point; `client-a` only exists under `tests/fixtures/agents/overrides/deployments/`) | Confirmed | No PR should attempt this move — see Affected Areas "No action" rows; the real in-package-example gap is `src/agents_system/demo.py`, not `deployments/` |
| Locator generalization touches `resolve()`'s core control flow used by every existing test and both shipped role trees | Medium | PR1 ships zero new public API, tests-first, and the full existing predefined-role/deployment-override test suite is the regression gate before any Agent API PR lands |
| `create_app`/runtime-id PR is large (lifespan loop + settings + adapter deletion together) | High (exploration already flagged review-budget risk here) | Split into a wiring PR and a settings/migration PR (see PR Split); `ask-on-risk` delivery strategy |
| Guard 1's "what counts as released" reference has no existing tooling (free-text `version:`, no prior diff mechanism) | Medium | sdd-design settles this explicitly before Guard 1 tasks are written (exploration Q6); default candidate is a checked-in snapshot fixture, following the existing `EXPECTED_ROLE_TOOLS` precedent |
| `permission-hierarchy` capability spec was never promoted from the archived change folder to `openspec/specs/` | Low, process-only | Flagged for sdd-spec; does not block this proposal, since the new capabilities here reference it by its actual archived path |

## Rollback Plan

Each PR is independently revertible (`git revert`) because the locator generalization, `Agent` API, and registration changes are additive/parallel call sites layered on top of `resolve`/`merge`/`build_runtime`/the injector — none of which change behavior for the platform-role path when no locator override is supplied. No persisted-state or database schema changes are involved anywhere in this change; permissions and role definitions remain process-config. Revert order follows the PR chain in reverse (latest slice first) since later PRs depend on earlier ones (e.g. registration redesign depends on the locator core); reverting the registration PR alone restores the deleted parsers' git history without touching the locator or Agent API PRs, if the runtime-id migration alone needs to be rolled back post-release. Guard 1's contract test can be reverted independently of everything else — it only adds a CI check, no runtime behavior.

## PR Split (ask-on-risk, ~400 lines/PR, Feature Branch Chain: PR1 targets the tracker branch, each later PR targets the immediate previous PR's branch)

1. **PR1 — Locator core.** `_role_folder`/`_load_role_files`/`_resolve_role_chain`/`load_generic`/`resolve()` accept the platform-role | importer-folder | inline-definition locator; `_extends_target` fails loudly outside the allowed spaces. No new public API. Tests first: locator-resolution unit tests (folder locator, inline locator, fail-loud `extends:`), full existing predefined-role/deployment-override suite green unchanged. ~350-400 lines.
2. **PR2 — `Agent` Python API.** `Agent.from_folder(...)`/`Agent(...)` builder module; exported via `__init__.py`; Option B additive inheritance (extend generic or a predefined role). Tests first: both entry shapes, combined folder+params, extends-a-predefined-role case. ~300-350 lines.
3. **PR3 — Skills for importer agents.** Extend `_load_skills` with importer-folder/inline sources; fix the `deployment.md`/`role.md` doc contradiction. Tests first. ~200-250 lines + docs.
4. **PR4a — Registration wiring.** `create_app` accepts `{id: Agent}`; lifespan loop rewritten around it. Tests first: registration tests, WhatsApp/adapter binding behavior preserved for predefined roles. ~350-400 lines.
5. **PR4b — Runtime-id deletion + env migration.** Delete `to_model_id`/`parse_model_id` and the inline parser; migrate `ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID`/`DEPLOY_GRANTS`-key semantics; migration notes in `CHANGELOG.md`. Tests first: `test_main.py` env-driven boot migration cases. ~250-300 lines.
6. **PR5 — Terminology rename + example relocation.** generic→predefined rename across code comments/docs; move `demo.py`/`demo-entrypoint.md` to `examples/`. Mostly docs; low risk. ~150-200 lines.
7. **PR6 — Guard 1 governance.** Versioned tools/permissions snapshot extending `tests/platform_role_contract.py`; contract test requiring SemVer bump + CHANGELOG entry; CI wiring. Tests first. ~300-350 lines.
8. **PR7 — ADR-004 + ADR-002 amendments.** `docs/architecture/adr-004-library-first-agents.md`; pointer amendments to ADR-002 A.1/D/C.15/E.18/F. Docs-only, no source change. ~150-200 lines.

## Success Criteria

- [ ] An importer can build an `Agent` from a folder, from Python params, or both, and it resolves to the same `AgentDefinition` shape a predefined role produces
- [ ] A custom agent can extend either the generic agent (`platform/roles/agent`) or any predefined role; every library invariant (B.8/B.9 contract, C.10 tiers, C.11 `untrusted_input`, T3 sandbox, ADR-003 grant rules) applies without agent-specific code
- [ ] `extends:` fails loudly (never silently collapses) for any value outside the two allowed locator spaces
- [ ] `create_app` serves a `{id: Agent}` registration; the 8 predefined roles and existing deployment overrides resolve and serve with unchanged behavior
- [ ] `to_model_id`/`parse_model_id` and the inline `main.py` parser are deleted; no duplicated runtime-id parsing remains anywhere
- [ ] Guard 1's contract test fails a predefined-role tools/permissions change that lacks a `version:` MAJOR bump and a `CHANGELOG.md` entry, and passes one that has both
- [ ] `docs/platform/deployment.md` and `docs/platform/role.md` no longer contradict each other on skills location
- [ ] `src/agents_system/demo.py` no longer ships inside the installable package
- [ ] ADR-004 accepted; ADR-002 A.1/D/C.15/E.18/F point to it
- [ ] Every PR ships tests-first, green, with docs updated, within or explicitly chained against the 400-line budget
