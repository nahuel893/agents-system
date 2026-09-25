# Verify Report: permission-model

## Scope

- **Change**: `permission-model` — class-based `Permission`/`Tier` hierarchy replacing string-prefix invariants; fixes issue #38.
- **Branch/commit inspected**: `origin/main` @ `d3fad1f` (worktree `perm-archive`, branch `docs/permission-model-archive`, no source diff from `origin/main`).
- **Shipped PRs**: #43 (PR1, registry), #50 (PR2, ToolSpec/injector/loader), #55 (PR3, grants + issue #38 fix), #57 (PR4, ADR-003 docs) — all merged. Bonus follow-up PR #58 (closes issue #47, the evals implicit-grant follow-up flagged as out of scope by this change's Resolved Decision 4) has also merged.
- **Artifacts inspected**: `proposal.md`, `specs/permission-hierarchy/spec.md`, `design.md` (Resolved Decisions), `tasks.md` (21/21 tasks checked `[x]`, `state.yaml` phase `tasks`), Engram `sdd/permission-model/spec` (obs #647), `sdd/permission-model/design` (obs #648), `sdd/permission-model/tasks` (obs #650).
- **Method**: `codegraph_explore` against the `perm-archive` worktree (CodeGraph index freshly initialized for this worktree) plus targeted `Read`/`rg` for line ranges CodeGraph truncated; source read is verbatim on-disk code, not summarized.
- **TDD mode**: Strict TDD was in effect for `apply` per `tasks.md`'s Cross-PR Ground Rules; this verify pass checks current shipped behavior and existing tests, not historical RED/GREEN execution (not observable after the fact).

## Commands Run

| Command | Result |
|---|---|
| `PATH=.../.venv/bin:$PATH PYTHONPATH=.../perm-archive/src pytest -q` | **1306 passed, 93 deselected, 18 xfailed**, 7 warnings (pre-existing, unrelated to this change — async-mock coroutine warning in `test_audit_sink.py`, SQLAlchemy savepoint warnings in health-check tests) |
| `ruff check .` | **All checks passed!** |
| `mypy src/` | **Success: no issues found in 69 source files** |

## Traceability: spec requirement → implementing code → covering tests → verdict

| Requirement | Implementing code | Covering test(s) | Verdict |
|---|---|---|---|
| Permission base class carries an explicit tier | `permissions/base.py:39-49` (`Permission.__init_subclass__`) | `tests/test_permissions_base.py` | PASS |
| Built-in action classes and their tiers | `permissions/builtins.py:19-46` (`Read` T0, `Write`/`Send`/`Run` T2, `Exec` T3) | `tests/test_permissions_builtins.py` | PASS |
| Unique wire name per class; explicit resource-level registration | `permissions/permission_registry.py:36-70` (`register`/`_register_locked`), `permissions/builtins.py:65-73` (`resource()`) | `tests/test_permissions_registry.py`, `tests/test_permissions_builtins.py` | PASS |
| Open hierarchy for downstream extension | `permissions/builtins.py:49-54` (`Spawn(Permission)`, new top-level family), `resource()` factory | `tests/test_permissions_builtins.py` | PASS |
| Subclass tier monotonicity (R1) | `permissions/base.py:39-59` | `tests/test_permissions_base.py` | PASS |
| ToolSpec permission tier ceiling (R2a) | `permissions/permission_registry.py:133-156` (`evaluate_tool_spec`), `harness/registry.py:70-89` (`ToolSpec.__post_init__`) | `tests/test_capability_tiers.py` | PASS |
| ToolSpec permission tier floor (R2b) | `permissions/permission_registry.py:158-161` | `tests/test_capability_tiers.py`, `tests/test_operator_connectors.py` | PASS |
| Grant coverage of permission descendants (R3) | `permissions/permission_registry.py:164-175` (`covers()`) | `tests/test_harness_injector.py` | PASS |
| untrusted_input roles hold no T3 permission (R4) | `harness/injector.py:88-96` (tool-exec barrier), `harness/loader.py:1580-1608` (`_validate_untrusted_input_exec`, load-time), `harness/factory.py:344-358` (`build_runtime`, grant-time) | `tests/test_harness_injector.py`, `tests/test_untrusted_input_invariant.py`, `tests/test_harness_factory.py`, `tests/test_main.py` | PASS |
| Explicit registration, no inference from string structure | `permissions/permission_registry.py:94-100` (`resolve`) | `tests/test_permissions_registry.py` | PASS |
| Name/class uniqueness and collision | `permissions/permission_registry.py:52-70` (`_register_locked`) | `tests/test_permissions_registry.py` | PASS |
| Lookup by name and by class | `permissions/permission_registry.py:94-108` (`resolve`/`reverse`) | `tests/test_permissions_registry.py` | PASS |
| Registry thread-safety | `permissions/permission_registry.py:34` (single `threading.Lock` guarding both maps), `:72-92` (`get_or_register`) | `tests/test_permissions_registry.py` | PASS |
| Accepted grant forms | `harness/factory.py:332-342` (`build_runtime`, mixed str/class resolution) | `tests/test_harness_factory.py` | PASS |
| No automatic grant | `src/agents_system/main.py:367-374` (`build_runtime(..., granted_permissions=settings.deploy_grants[model_id])`; no reference to `definition.permissions` as a grant source remains) | `tests/test_main.py` | PASS |
| Boot failure without an explicit grant | `main.py:350-359` (`DefinitionError` naming `model_id`/role) | `tests/test_main.py` | PASS |
| Layer-2 revalidates against the persisted deploy grant ceiling (issue #38) | `harness/interceptor.py:146-166` (`intercept`, ceiling ∩ current), `agent/graph.py:468-481` (`run_turn` default) | `tests/test_issue_38_regression.py`, `tests/test_harness_interceptor.py`, `tests/test_agent_runtime.py` | PASS |
| Wire format stability across manifests, YAML, logs, audit | No `platform/roles/*/manifest.md` edited by PR1-PR4 (confirmed: none of the four PRs' file lists touch `platform/roles/`); string names still logged (`injector.py`'s `logger.warning(..., reason=reason)` uses wire names, not classes) | `tests/test_untrusted_input_invariant.py` (all-manifest load), full suite | PASS |
| Permission error type hierarchy | `permissions/errors.py:26-184` (`AgentPermissionError` root + 6 subclasses) | `tests/test_permissions_base.py`, `tests/test_permissions_registry.py`, `tests/test_capability_tiers.py`, `tests/test_harness_injector.py`, `tests/test_untrusted_input_invariant.py` | PASS — see Documented Deviation 1 (root class name) |
| R2a/R2b compatibility of every currently shipped ToolSpec | `permissions/builtins.py:57-62,143` (`ReadFiles(Read)` at T3, registered under `read:files`) — resolves the `read_file` R2b failure the spec table predicted | `tests/test_operator_connectors.py`, `tests/test_capability_tiers.py` | PASS |
| PR4: ADR-003 written, ADR-002 amended, `permission-model.md` cross-referenced | `docs/architecture/adr-003-permission-hierarchy.md` (exists, 7.4k); `docs/architecture/adr-002-agent-model-and-capabilities.md` has exactly 4 "Superseded by ADR-003" pointers at lines 145, 656, 718, 808; `docs/architecture/permission-model.md:123` cross-references ADR-003 | docs-only, no test command applies | PASS |

**Verdict counts: 21 PASS, 0 PARTIAL, 0 FAIL.**

## Documented Deviations (known, non-blocking)

1. **`AgentPermissionError` name.** The on-disk `specs/permission-hierarchy/spec.md` (as read from the worktree) already names the error root `AgentPermissionError`, "named to avoid shadowing the built-in `PermissionError`, an `OSError` subclass already caught elsewhere in this codebase (e.g. `connectors/operator.py`'s process-group cleanup)" — and the shipped code (`permissions/errors.py:26`) matches exactly. The only discrepancy found is that the **Engram-cached** spec observation (`sdd/permission-model/spec`, obs #647, created 2026-09-24 16:10) still shows the earlier draft name `PermissionError` as the exception root — Engram was not re-synced after the on-disk file was updated. This is store staleness, not a code deviation; the openspec file (source of truth for this hybrid-store change) already reflects shipped behavior.
2. **Grant ceiling = granted classes covering a declared permission.** The spec's "Layer-2 revalidates against the persisted deploy grant ceiling" requirement describes the ceiling only as "the deploy-time grant ceiling." The shipped implementation (`harness/factory.py:360-384`) narrows this: `deploy_grant_ceiling` is the raw grant set filtered to classes that R3-cover at least one permission the role's `AgentDefinition` actually *declares* — a `DEPLOY_GRANTS`/`granted_permissions` entry naming a permission the role never declares neither equips a tool nor enters the ceiling. In-code comments attribute this to "PR #55 security review (MEDIUM-HIGH)." Consistent with, and a safety-tightening refinement of, the spec text — not a violation.
3. **Class/name grant equivalence.** `harness/factory.py:386-398` derives `covered_declared_names` (the role's own wire-name strings covered by the grant) specifically so a class-form grant (e.g. granting `Write` directly) and its string-form equivalent (`"write:orders"`) equip Layer-1 (`resolve_tool_surface`) identically. Documented in-code as a PR #55 review fix (a class object would otherwise never string-equal a wire name). Matches spec's "Accepted grant forms" intent; not explicitly spelled out in the spec text.
4. **`tier_rank`.** `permissions/base.py:16-29` defines an explicit `_RANK: dict[Tier, int]` and `tier_rank()` helper, used by every tier comparison in the package instead of relying on `Tier(str, Enum)`'s inherited string ordering — a robustness choice not mandated by the spec's predicate wording (`t >= p.tier`), but semantically equivalent for `T0`-`T3`.
5. **Atomic `get_or_register`.** `permissions/permission_registry.py:72-92` adds a `get_or_register(name, factory)` beyond the spec's plain `register`/`resolve`, closing a TOCTOU window for wire names discovered at runtime under concurrency (used by `harness.loader` for manifest-declared `command_tools:` permissions). Additive; does not change any spec-described behavior of `register`/`resolve`.
6. **Deferred imports.** `harness/registry.py:87` and multiple sites in `harness/loader.py` import `agents_system.permissions` inside function bodies rather than at module scope, to avoid a circular import (`permissions.base` imports `Tier` from `harness.registry`, which transitively imports `harness.loader`). An implementation detail, invisible to any spec-described behavior.

## Open Follow-ups (not part of this change; noted for future tracking)

- **Library-only unregistered-class case failing at turn time.** `harness/factory.py:293,340-342` (`build_runtime`) accepts a bare `Permission` subclass in `granted_permissions` without requiring it be registered in `PermissionRegistry` — only string entries are resolved through the registry; a class entry passes through unchanged and can be stored in `EquippedRuntime.deploy_grant_ceiling`. `AgentRuntime.run_turn`'s no-override default (`agent/graph.py:475-480`) later calls `permission_registry.reverse(cls)` for every class in that ceiling to rebuild the string-form `effective_permissions`; for a class that was never registered, this raises `UnknownPermissionNameError` — but only at first turn execution, not at grant or boot time. This gap is specific to the **library path** (`build_runtime` called directly with a class grant); `main.py`'s deployment path only ever supplies strings (`DEPLOY_GRANTS` JSON decoding), so it cannot hit this. Not covered by any task in `tasks.md`, and not a requirement in `spec.md`. Recommend filing a GitHub issue (same pattern as issue #47, which tracked and closed the evals implicit-grant follow-up via PR #58).
- **Evals implicit full-grant default (design.md Resolved Decision 4)** — explicitly out of scope for `permission-model`. For traceability: this *was* filed (issue #47) and has already been fixed by PR #58 (merged), independently of this change's four PRs. No action needed here; noted so the archive record does not describe it as still-open.

## Summary

Every numbered requirement (R1, R2a, R2b, R3, R4) and every named requirement in `specs/permission-hierarchy/spec.md` has an identifiable implementation and a passing covering test on `origin/main`. Full suite (1306 tests), `ruff check`, and `mypy src/` are all clean. Six deviations from the literal spec draft are documented above; all are either (a) already reflected in the on-disk spec text (deviation 1), or (b) safety-preserving/tightening implementation choices consistent with the spec's intent and explicitly justified in code comments (deviations 2-6) — none constitutes a FAIL. One open follow-up (library-path unregistered-class grant) is newly identified by this verify pass and was not previously tracked; it does not block archiving this change, which is documentation/behavior already shipped and merged.

**No FAIL findings. Proceeding to archive.**
