# Proposal: Permission class hierarchy replacing prefix-string invariants

## Intent

Permission safety today is inferred from string prefixes (`exec:`, `write:`, `send:`, `run:`) checked in three independent places (`ToolSpec.__post_init__`, the C.11 `untrusted_input` exec-prefix check, the injector's T3 barrier). A downstream consumer cannot add a new dangerous action without either colliding with a reserved prefix or falling outside all safety checks. Replace the prefix convention with an open `Permission` class hierarchy carrying an explicit `Tier`, so danger is declared per class and enforced structurally, not inferred from spelling. This also fixes issue #38 (Layer-2 revalidates the role's full permission set, not the effective grant), whose root cause — no persisted grant ceiling distinct from role permissions — this change removes by construction.

## Scope

### In Scope
- `Permission` base class; built-ins `Read`, `Send`, `Write`, `Exec`, `Run` mapping current prefixes; hierarchy open to subclassing and new top-level actions.
- R1 (subclass tier ≥ parent tier, checked at class creation), R2 (`ToolSpec` tier/permission-tier ordinal predicate replacing prefix branches), R3 (grant-of-`P`-covers-descendant-`D` only if `D.tier <= P.tier`), R4 (`untrusted_input` role holds no T3 permission, any name).
- Explicit package-global `PermissionRegistry` (name ↔ class), fail-loud on unknown name / collision.
- Explicit deploy-time grants (classes or names); remove AD-5's auto-grant of the full role set in `main.py`; fix #38 by persisting the deploy grant ceiling in `EquippedRuntime` and using it in Layer-2.
- Backward-compatible string wire format for manifests, YAML eval scenarios, logs, audit.
- ADR-003 (amends ADR-002's prefix invariants, C.11, grant model).

### Out of Scope
- `library-first-agents` / ADR-004 (Agent API, folders, extends, runtime ids).
- Per-principal identity (issue #15, ADR-002 A.2) — grants stay role/deploy-scoped, not per-caller.
- Entry-point/plugin permission discovery (registry stays explicit-import only).

## Capabilities

### New Capabilities
- `permission-hierarchy`: class-based `Permission`/`Tier` model, `PermissionRegistry`, R1–R4 enforcement, replacing the prefix-invariant behavior currently undocumented as a spec.

### Modified Capabilities
None — `openspec/specs/` has no existing capability to delta against.

## Approach

Hybrid: strings remain the canonical wire format at manifests/YAML/logs/audit; a `PermissionRegistry` resolves name ↔ `type[Permission]` at ingress. Policy code (`ToolSpec.__post_init__`, loader validation, injector, interceptor) operates on resolved classes. `Permission.__init_subclass__` enforces R1. `ToolSpec.__post_init__` (`harness/registry.py:83-117`) resolves each required wire name and compares class `tier` via the ordinal predicate (R2) instead of prefix branches. Injector `_deny_reason` (`harness/injector.py:77-99`) uses `issubclass` + tier comparison for R3 grant coverage and denies any T3 class for `untrusted_input` (R4), generalizing the existing T3 barrier. Loader's `_is_exec_permission`/`_validate_untrusted_input_exec` (`harness/loader.py:1498-1585`, applied at `:1699-1703,1852-1860`) is replaced by class-based R4 validation at both `resolve()` return paths. `EquippedRuntime` (`harness/factory.py:76-96`) gains the persisted deploy grant; `main.py:299-329` stops passing `definition.permissions` as the grant; `AgentRuntime.run_turn`/`intercept` (`agent/graph.py:395-476`, `harness/interceptor.py:69-153`) revalidate against that ceiling.

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `src/agents_system/permissions/` (new) | New | `Permission`, `Tier` reuse, `PermissionRegistry`, built-in action classes |
| `harness/registry.py:8-117` | Modified | `ToolSpec.__post_init__` ordinal predicate (R2) replaces prefix branches |
| `harness/injector.py:77-99,172-224` | Modified | `_deny_reason` uses class/tier comparison (R3, R4) |
| `harness/loader.py:207,274,1423-1467,1498-1585,1692-1703,1852-1860` | Modified | Manifest/deployment permission resolution to classes; R4 validation |
| `harness/factory.py:76-96` | Modified | `EquippedRuntime` persists the deploy grant ceiling |
| `harness/interceptor.py:35-153` | Modified | Layer-2 revalidates against the persisted ceiling (fixes #38) |
| `agent/graph.py:395-476` | Modified | `run_turn` default uses persisted ceiling, not `definition.permissions` |
| `src/agents_system/main.py:299-360` | Modified | Remove AD-5 auto-grant; explicit deploy-time grant call |
| `src/agents_system/__init__.py:15-37` | Modified | Public example updated to class-based grant |
| `docs/architecture/adr-003-*.md` (new), `adr-002-*.md` | New/Modified | ADR-003; ADR-002 prefix/C.11/grant sections marked superseded |
| `tests/test_capability_tiers.py`, `test_harness_injector.py`, `test_harness_interceptor.py`, `test_untrusted_input_invariant.py`, `test_harness_loader.py`, `test_main.py`, `test_command_tools_injector.py` | Modified | Prefix/string assertions → class/tier equivalents; add #38 regression |

## Backward Compatibility & Migration

Manifests, `command_tools:` YAML, eval scenario YAML, logs, and audit payloads keep string names unchanged (`read:catalog`, `write:orders`, etc.) — the registry resolves them; no shipped manifest under `platform/roles/` needs edits. `deployments/` currently has no override manifests, so no deployment migration is required now, only forward compatibility for future overrides. Any consumer calling public APIs with string permission names keeps working; class-typed grants are additive. The one behavioral break is deliberate: `main.py` boot no longer auto-grants a role's full permission set — every deployment must state its grant explicitly, or the app fails to boot with a clear "no grant configured" error (not a silent full-grant).

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| R2 ordinal direction ambiguity (exploration flagged the literal wording as inverted) | Medium | Spec phase writes the exact predicate and acceptance examples before any code |
| Grant-ceiling change silently narrows a working deployment at boot | Medium | `main.py` PR ships with an explicit migration note and a boot-time error naming the missing grant |
| String/class split-brain across ingress points | Low | Single canonical registry; normalize once at each ingress, never compare raw strings after conversion |
| Review size exceeds 400-line budget | High (per exploration) | PR split below; `ask-on-risk` |

## Rollback Plan

Each PR is independently revertible (`git revert`) because grants, `ToolSpec` construction, and loader validation stay behind the same public call sites; a revert restores prefix-string checks and the AD-5 auto-grant. No data migration, no persisted-state schema change — permissions are process-config, not stored rows. Revert PR3 alone if the grant-ceiling change breaks a boot path; PR1/PR2 are safe to keep since they only change internal validation, not the wire format.

## PR Split (ask-on-risk, ~400 lines/PR)

1. **PR1 — Model + registry.** `Permission`/`Tier`, built-in classes, `PermissionRegistry`, R1. Tests first: registry resolve/collision/unknown-name, R1 tier-monotonicity. No behavior change to existing code paths.
2. **PR2 — `ToolSpec` + injector migration.** R2 ordinal predicate replaces prefix branches in `registry.py`; injector `_deny_reason` R3/R4 via class/tier. Tests first: `test_capability_tiers.py`, `test_harness_injector.py`, `test_untrusted_input_invariant.py` rewritten to class equivalents plus new R3 descendant-tier cases. Docs: `permission-model.md` (if present) updated.
3. **PR3 — Grants + Layer-2 fix + `main.py`.** `EquippedRuntime` grant ceiling; `main.py` explicit grant, AD-5 removed; interceptor/`run_turn` use ceiling. Tests first: `test_main.py`, `test_harness_interceptor.py`, new #38 regression (narrower deploy grant vs. wider role permissions). Docs: ADR-003 draft cross-links this PR.
4. **PR4 — Docs + ADR-003.** ADR-003 finalized; ADR-002 prefix/C.11/grant sections marked superseded with pointer. No source change.

## Success Criteria

- [ ] R1–R4 enforced and covered by tests; every prior prefix-based test has a class/tier equivalent
- [ ] Issue #38 has a passing regression test (narrower grant denied at Layer-2 even when role permits)
- [ ] AD-5 auto-grant removed from `main.py`; boot fails loudly without an explicit grant
- [ ] All shipped manifests under `platform/roles/` load unchanged
- [ ] ADR-003 accepted; ADR-002 amended sections point to it
- [ ] Each PR ships green with tests-first and docs updated, within/near the 400-line budget or explicitly chained
