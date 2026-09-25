# Tasks: Permission class hierarchy replacing prefix-string invariants

Source artifacts: `proposal.md`, `specs/permission-hierarchy/spec.md`, `design.md`
(see `design.md`'s **Resolved Decisions** section — owner-approved 2026-09-24,
mirrored at Engram `sdd/permission-model/design`).

Test runner: `.venv/bin/pytest -q`. Lint: `.venv/bin/ruff check` and
`.venv/bin/ruff format --check`. Every task's verification command MUST be run
from the repo root before the task is checked off.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1,230 total (PR1 ~350, PR2 ~390, PR3 ~340, PR4 ~150) |
| 400-line budget risk | Medium |
| Chained PRs recommended | Yes |
| Suggested split | PR1 → PR2 → PR3 → PR4 |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main |

```text
Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium
```

PR1 (~350) and PR2 (~390) are both close to the 400-line ceiling per
`design.md`'s own Testing-Strategy table — a scope slip on either (e.g. more
R2b edge-case tests than estimated, or a wider `_deny_reason` rewrite than
expected) crosses it. If either PR's actual diff approaches 400 lines during
apply, split its tests-only portion into a preceding chained PR rather than
widening the budget silently.

## Cross-PR Ground Rules

- Each PR MUST be independently mergeable: full suite green
  (`.venv/bin/pytest -q`), `.venv/bin/ruff check` clean, `.venv/bin/ruff
  format --check` clean, before its closing task is checked off.
- Strict TDD by convention for every new/rewritten test in this change
  (`design.md`'s Testing Strategy section), even though
  `openspec/config.yaml`'s repo-wide `rules.apply.tdd` stays `false`. RED
  before GREEN, always run the RED test first and confirm it fails for the
  stated reason.
- No task in this file introduces or depends on evals' implicit full-grant
  default (`evals/schema.py:56-65`, `evals/runner.py:347-383`) — Resolved
  Decision 4 keeps it explicitly out of scope. File a separate follow-up
  GitHub issue for it; do not fold it into any task below.
- No manifest under `platform/roles/*/manifest.md` is edited by any task
  below — wire format stability is a hard requirement (spec: "Wire format
  stability across manifests, YAML, logs, audit").

---

## PR1 — Permission model + registry

Branch: `feat/permission-model-1-registry`
Scope: new `src/agents_system/permissions/` package (`base.py`, `errors.py`,
`builtins.py`, `permission_registry.py`, `__init__.py`). No existing source
file outside this new package is touched. Estimated lines: ~350 (design.md
Testing Strategy table).

- [x] **PR1-T1 — `Permission` root, `__init_subclass__` (R1), error hierarchy.**
  RED: write `tests/test_permissions_base.py` covering spec Requirement
  "Permission base class carries an explicit tier" and "Subclass tier
  monotonicity (R1)": concrete subclass declaring `tier = Tier.T1` is valid;
  concrete subclass omitting `tier` raises `InvalidPermissionTierError`;
  equal-tier subclass valid; escalating subclass valid; de-escalating
  subclass raises `InvalidPermissionTierError` *before the class object
  exists* (assert via `pytest.raises` around the `class` statement itself,
  not a later call); subclass omitting a tier override inherits the
  parent's declared tier (`cls.__dict__.get("tier")` distinguishes this
  from an explicit lower tier, per `design.md`'s Class API).
  GREEN: create `src/agents_system/permissions/errors.py` with
  `AgentPermissionError` root and `InvalidPermissionTierError` (only this one
  subclass needed for this task; the rest of the tree lands in later PR1
  tasks). Create `src/agents_system/permissions/base.py` with the
  `Permission` class exactly per `design.md`'s Class API section
  (`__init_subclass__`, `_RANK` ordinal map over `Tier` imported from
  `agents_system.harness.registry`).
  Verify: `.venv/bin/pytest -q tests/test_permissions_base.py`
  Satisfies: "Permission base class carries an explicit tier",
  "Subclass tier monotonicity (R1)". <!-- sdd-owner: implementation -->

- [x] **PR1-T2 — Built-in action classes, `resource()` factory, full wire-name table.**
  RED: write `tests/test_permissions_builtins.py` covering spec Requirement
  "Built-in action classes and their tiers" and "Open hierarchy for
  downstream extension": `Read`=T0, `Write`=T2, `Send`=T2, `Exec`=T3,
  `Run`=T2 all exist and are `Permission` subclasses; every one of the 18
  shipped wire names (the 12-row R2a/R2b compatibility table in `spec.md`
  minus non-permission rows, plus the 6-name registration gap from
  `design.md`: `read:session`, `read:price_lists`, `write:session`,
  `write:summary_output`, `spawn:sales-agent`, `spawn:data-agent`,
  `spawn:summary-agent`) resolves to a distinct registered subclass, and
  none of them is the bare action-root class itself; a downstream-style
  subclass of `Write` (e.g. `ExportWrite(Write): tier = Tier.T2`) registers
  and resolves like a built-in (Resolved Decision 2's own worked example);
  `Spawn(Permission)` is a genuinely new top-level family at `Tier.T2` per
  Resolved Decision 2, and its three `spawn:*` wire names resolve to
  distinct `Spawn` subclasses; `read:files` resolves to a dedicated
  `ReadFiles(Read)` subclass with `tier = Tier.T3` (an explicit escalation
  over `Read`'s T0), per Resolved Decision 1 — assert `ReadFiles.tier is
  Tier.T3` and `issubclass(ReadFiles, Read)` directly in this test file,
  since the classification itself (not its R2a/R2b consequence, which is
  PR2's concern) lands here.
  GREEN: create `src/agents_system/permissions/builtins.py` with the five
  action roots (`Read`, `Write`, `Send`, `Exec`, `Run`) plus `Spawn`
  (Resolved Decision 2), the `resource(parent, wire_name, *, tier=None)`
  factory exactly per `design.md`, `ReadFiles(Read)` with `tier =
  Tier.T3` (Resolved Decision 1) constructed directly (not via `resource()`,
  since it needs a tier override rather than inheriting), and the flat
  registration table covering all 18 wire names.
  Verify: `.venv/bin/pytest -q tests/test_permissions_builtins.py`
  Satisfies: "Built-in action classes and their tiers", "Open hierarchy for
  downstream extension". <!-- sdd-owner: implementation -->

- [x] **PR1-T3 — `PermissionRegistry`: resolve/reverse/register, collision, thread-safety.**
  RED: write `tests/test_permissions_registry.py` covering spec Requirements
  "Explicit registration, no inference from string structure", "Name/class
  uniqueness and collision", "Lookup by name and by class", "Registry
  thread-safety": `resolve()` returns the registered class;
  `resolve()` of an unregistered name raises `UnknownPermissionNameError`
  naming the exact string; re-registering the exact same class under the
  exact same name is idempotent (no error, no duplicate); registering a
  different class under an already-bound name raises
  `PermissionRegistrationCollisionError` naming both classes; registering
  the same class under a second different name also raises
  `PermissionRegistrationCollisionError` (one canonical name per class);
  reverse resolution (class → name) returns the canonical name, and reverse
  resolution of a never-registered class raises `UnknownPermissionNameError`;
  a `ThreadPoolExecutor`-driven concurrent-resolve test against the
  already-registered built-ins returns a correct class from every thread
  with no corruption.
  GREEN: create `src/agents_system/permissions/permission_registry.py`:
  `PermissionRegistry` with `_by_name: dict[str, type[Permission]]`,
  `_by_class: dict[type[Permission], str]`, one `threading.Lock` guarding
  both read and write paths, `resolve(name)`, `reverse(cls)`,
  `register(cls, name)`; add `UnknownPermissionNameError` and
  `PermissionRegistrationCollisionError` to `permissions/errors.py`; create
  the package-global `permission_registry` instance.
  Verify: `.venv/bin/pytest -q tests/test_permissions_registry.py`
  Satisfies: "Explicit registration, no inference from string structure",
  "Name/class uniqueness and collision", "Lookup by name and by class",
  "Registry thread-safety". <!-- sdd-owner: implementation -->

- [x] **PR1-T4 — Test-isolation fixture: registry snapshot/restore.**
  RED: add a test to `tests/test_permissions_registry.py` (or a focused new
  test in the same file) proving cross-test isolation: register a
  throwaway class under a throwaway name inside one test function that uses
  the fixture, assert in a *second* test function (same file, run after the
  first in the same session) that the throwaway name is NOT resolvable —
  this fails RED against a registry with no reset mechanism (the first
  test's registration would otherwise leak into the second).
  GREEN: add test-only `_snapshot()` / `_restore(snapshot)` methods to
  `PermissionRegistry` (leading-underscore, documented as test-only, per
  `design.md`'s Test Isolation note — copy/restore both `_by_name` and
  `_by_class` under the same lock). Add a `reset_permission_registry`
  fixture to `tests/conftest.py` that snapshots before and restores after a
  test (NOT global/autouse — apply it only to `test_permissions_*.py` via
  `pytestmark = [pytest.mark.usefixtures("reset_permission_registry")]` at
  the top of `test_permissions_base.py`, `test_permissions_builtins.py`,
  and `test_permissions_registry.py`, added by this task since it is the
  cross-cutting piece all three PR1 test files depend on).
  Verify: `.venv/bin/pytest -q tests/test_permissions_base.py
  tests/test_permissions_builtins.py tests/test_permissions_registry.py`
  Satisfies: `design.md`'s Test Isolation design note (no spec requirement
  ID — a design-phase testing infrastructure decision explicitly called out
  for this task in the phase brief). <!-- sdd-owner: implementation -->

- [x] **PR1-T5 — Package public surface: `permissions/__init__.py`.**
  RED: write a small assertion set (append to
  `tests/test_permissions_builtins.py` or a new
  `tests/test_permissions_package_exports.py` — implementer's choice, keep
  it short) that `from agents_system.permissions import Permission,
  PermissionRegistry, Read, Write, Send, Exec, Run, Spawn` succeeds, that a
  module-level `resolve()` and `register()` are importable and delegate to
  the package-global `permission_registry` instance, and that
  `AgentPermissionError` plus every error subclass defined so far
  (`InvalidPermissionTierError`, `UnknownPermissionNameError`,
  `PermissionRegistrationCollisionError`) are importable from
  `agents_system.permissions`.
  GREEN: populate `src/agents_system/permissions/__init__.py` with the
  re-exports exactly per `design.md`'s Module Layout comment.
  Verify: `.venv/bin/pytest -q tests/test_permissions_base.py
  tests/test_permissions_builtins.py tests/test_permissions_registry.py`
  (plus the new export test file if created separately)
  Satisfies: "Open hierarchy for downstream extension" (import surface a
  downstream package relies on). <!-- sdd-owner: implementation -->

- [x] **PR1-T6 — PR1 closing: docs + full verification.**
  Add a module docstring to each of `base.py`, `builtins.py`,
  `permission_registry.py`, `errors.py`, `__init__.py` explaining its role
  in the R1 hierarchy (one paragraph each, cross-referencing
  `design.md`'s Module Layout and the spec's requirement names — no new
  prose invented beyond what `design.md`/`spec.md` already state). No
  `CHANGELOG` file exists in this repo (confirmed: `find . -maxdepth 1
  -iname "CHANGELOG*"` returns nothing) — do not create one.
  Verify: `.venv/bin/pytest -q` (full suite, confirms PR1's new package
  does not break any existing test — none should reference it yet),
  `.venv/bin/ruff check`, `.venv/bin/ruff format --check`. Run `git diff
  --stat` against the PR1 branch base and confirm the total is at or below
  ~400 lines; if it exceeds 400, flag it in the PR description rather than
  silently merging oversized. <!-- sdd-owner: implementation -->

---

## PR2 — `ToolSpec` R2a/R2b + injector R3/R4 + command-tool tier ceiling

Branch: `feat/permission-model-2-toolspec`
Scope: `harness/registry.py`, `harness/injector.py`, `harness/loader.py`.
Depends on PR1 (imports `agents_system.permissions`). Estimated lines: ~390
(design.md Testing Strategy table) — closest PR to the 400-line ceiling;
watch scope closely (see Cross-PR Ground Rules).

- [x] **PR2-T1 — `ToolSpec` R2a (ceiling) replaces prefix branches.**
  RED: rewrite the prefix-based assertions in `tests/test_capability_tiers.py`
  to their R2a class/tier equivalents (spec Requirement "ToolSpec permission
  tier ceiling (R2a)"): `test_write_permission_with_t1_tier_raises`,
  `test_send_permission_with_t0_tier_raises`,
  `test_exec_permission_with_t2_tier_raises`,
  `test_whitespace_variant_write_permission_still_caught`,
  `test_run_permission_with_t0_tier_raises`,
  `test_run_permission_with_t1_tier_raises`,
  `test_run_permission_case_and_whitespace_variant_still_caught` all become
  assertions that construction raises `PermissionTierMismatchError` (not the
  current bare `ValueError`) when `t >= p.tier` is false, using registered
  permission names (no case/whitespace tolerance requirement carries over —
  that was a prefix-matching concern; a registered name is looked up
  exactly). Add the spec's own new scenarios: T3 tool requiring its
  exact-tier `Exec` permission passes; T2 tool requiring its exact-tier
  `Write` permission passes; the explicit regression-guard scenario ("`
  use_term` (T3) requiring `Write` (T2) must evaluate `t >= p.tier` — 3>=2
  true — and must NOT evaluate the inverted `p.tier >= t`).
  GREEN: add `PermissionTierMismatchError` to `permissions/errors.py`; add
  an `evaluate_tool_spec(name, tier, required_permissions)` callable to the
  `permissions` package (co-locate in `permission_registry.py` or a new
  small module — implementer's choice; it must resolve each string in
  `required_permissions` via the package-global registry, then check R2a),
  exported from `permissions/__init__.py`. In `registry.py`, replace the
  three `startswith` branches inside `ToolSpec.__post_init__` (currently
  `registry.py:94-124`, using constants `_WRITE_SEND_PREFIXES`,
  `_EXEC_PREFIX`, `_RUN_PREFIX` at `registry.py:42-44`) with a deferred
  import (`from agents_system.permissions import evaluate_tool_spec` inside
  `__post_init__`, matching the existing `injector.py:20-21` cycle-avoidance
  pattern) and a single call to it. Remove the now-unused prefix constants.
  Verify: `.venv/bin/pytest -q tests/test_capability_tiers.py`
  Satisfies: "ToolSpec permission tier ceiling (R2a)". <!-- sdd-owner: implementation -->

- [x] **PR2-T2 — `ToolSpec` R2b (floor) + `read_file` regression.**
  RED: extend `tests/test_capability_tiers.py` with the spec's R2b scenarios
  (Requirement "ToolSpec permission tier floor (R2b)"): a T3 tool requiring
  only a T0 permission fails the floor with `PermissionFloorViolationError`
  even though R2a alone passes; a T1 tool has no floor requirement; a T2
  tool with one T0 and one T2 required permission passes (`max(0,2)>=2`); a
  T0 tool with an empty `required_permissions` tuple has no floor
  requirement; `order_writer`'s own two `Write`-T2 permissions independently
  satisfy both R2a and R2b. Add the explicit regression case for Resolved
  Decision 1: construct a `ToolSpec` shaped like `read_file`
  (`tier=Tier.T3, required_permissions=("read:files",)`) and assert it now
  **succeeds** under both R2a and R2b, because `read:files` resolves to
  `ReadFiles(Read)` at `tier=Tier.T3` (registered in PR1-T2) — this is the
  test that actually exercises Resolved Decision 1 end-to-end, since PR1
  only asserted the classification in isolation.
  GREEN: extend `evaluate_tool_spec()` to also enforce R2b
  (`max(p.tier for p in required_classes) >= t` for `t in {Tier.T2,
  Tier.T3}`), raising the new `PermissionFloorViolationError` (add to
  `permissions/errors.py`) as a distinct sibling of
  `PermissionTierMismatchError`, not a reuse. No change to
  `connectors/operator.py`'s `read_file`/`use_term` `ToolSpec`
  declarations — they already declare `required_permissions=("read:files",)`
  / `("exec:command",)` unchanged; only the classification `read:files`
  resolves to (shipped in PR1) makes R2b pass now.
  Verify: `.venv/bin/pytest -q tests/test_capability_tiers.py
  tests/test_operator_connectors.py`
  Satisfies: "ToolSpec permission tier floor (R2b)",
  "R2a/R2b compatibility of every currently shipped ToolSpec".
  <!-- sdd-owner: implementation -->

- [x] **PR2-T3 — Injector R3 (descendant grant coverage) + R4 (T3 barrier, generalized).**
  RED: rewrite `tests/test_harness_injector.py`'s permission-coverage tests
  to class/tier equivalents and add the spec's R3 descendant-tier scenarios
  (Requirement "Grant coverage of permission descendants (R3)"): an ancestor
  grant (`Read`) covers a same-tier registered descendant
  (`issubclass` holds and tiers are equal); an exact-class grant always
  covers itself; an ancestor grant does NOT cover a descendant that
  escalated its own tier (e.g. a T2 override of a T0 parent) — the grant
  must be explicit for the escalated class; a grant of an unrelated class
  never covers by tier alone (`Write` granted does not cover a required
  `Exec`, regardless of tier ordering). Keep
  `test_untrusted_input_role_denied_t3_tool_even_with_permission_granted`
  and its siblings, restated to assert the barrier holds for ANY T3-tier
  class (spec Requirement "untrusted_input roles hold no T3 permission
  (R4)"), not only ones whose current permission name happens to be
  `exec:*`.
  GREEN: rewrite `_deny_reason` (`injector.py:77-99`): resolve
  `spec.required_permissions` and the caller's `effective` permission
  strings to classes via the registry, replace `set(spec.required_permissions)
  <= effective` with an R3 coverage check (`issubclass(required_cls,
  granted_cls) and required_cls.tier <= granted_cls.tier` for at least one
  granted class per required class), and keep the existing
  `definition.untrusted_input and spec.tier == Tier.T3` short-circuit
  (already class-tier-based, unchanged — this line already generalizes
  correctly).
  Verify: `.venv/bin/pytest -q tests/test_harness_injector.py`
  Satisfies: "Grant coverage of permission descendants (R3)",
  "untrusted_input roles hold no T3 permission (R4)".
  <!-- sdd-owner: implementation -->

- [x] **PR2-T4 — Loader R4: load-time and any-name T3 rejection for untrusted_input.**
  RED: rewrite `tests/test_untrusted_input_invariant.py`'s exec-prefix-named
  tests (`test_exec_prefix_check_is_case_and_whitespace_insensitive`,
  `test_no_override_branch_rejects_untrusted_input_with_exec_permission`,
  `test_merge_branch_rejects_untrusted_input_with_exec_permission`) to
  resolve permissions via the registry and assert rejection is driven by
  `.tier is Tier.T3`, not by an `exec:` string match; add the spec's new
  scenario: an `untrusted_input: true` role holding a differently-named T3
  permission (e.g. a hypothetical `run:destructive` resolved to a T3 class)
  is rejected at load exactly like `exec:command` is today, and assert the
  raised type is `UntrustedInputGrantError` (spec Requirement
  "untrusted_input roles hold no T3 permission (R4)": "load MUST raise
  UntrustedInputGrantError naming the offending permission and role").
  Update every test in this file currently asserting `DefinitionError` for
  this specific exec-rejection path to assert `UntrustedInputGrantError`
  instead; confirm (and note in the PR body) whether
  `UntrustedInputGrantError` also needs to subclass `DefinitionError` for
  any other caller in `loader.py` that catches `DefinitionError` broadly —
  check via `grep -n "except DefinitionError" src/agents_system/` before
  deciding, and make it a subclass if any such catch site depends on it.
  GREEN: add `UntrustedInputGrantError` to `permissions/errors.py`. Replace
  `_is_exec_permission` (`loader.py:1501-1508`) and
  `_validate_untrusted_input_exec` (`loader.py:1560-1585`) with a
  class-based check: resolve every name in `permissions` through the
  registry, reject if any resolved class has `.tier is Tier.T3`, raising
  `UntrustedInputGrantError` naming the offending wire name, its class, and
  `role_name`. Keep both existing call sites unchanged
  (`loader.py:1701-1703`'s merge branch, `loader.py:1858-1860`'s
  no-override branch).
  Verify: `.venv/bin/pytest -q tests/test_untrusted_input_invariant.py`
  Satisfies: "untrusted_input roles hold no T3 permission (R4)".
  <!-- sdd-owner: implementation -->

- [x] **PR2-T5 — Narrow declarative command-tool tier ceiling to T2 (Resolved Decision 3).**
  RED: add a test to `tests/test_command_tools_loader.py` asserting a
  `command_tools:` declaration with `tier: T3` is now rejected at load
  (previously accepted, since `_COMMAND_TOOL_MIN_TIER` allowed both T2 and
  T3); confirm the existing T2 declaration tests in
  `tests/test_command_tools_loader.py` and
  `tests/test_command_tools_injector.py` still pass unchanged (no shipped
  manifest declares `command_tools:` today, so this is a pure narrowing
  with zero live-manifest migration, per `design.md`'s Resolved Decision 3).
  GREEN: narrow `_COMMAND_TOOL_MIN_TIER` (`loader.py:366`) from
  `(Tier.T2, Tier.T3)` to `(Tier.T2,)`. No change to `Run`'s own tier (still
  T2, from PR1) or to `build_command_tool_spec`
  (`connectors/command_tools.py:237-250`) — the ceiling change alone is
  sufficient because `_COMMAND_TOOL_MIN_TIER` is exactly what
  `loader.py:607` checks against a declared `tier`.
  Verify: `.venv/bin/pytest -q tests/test_command_tools_loader.py
  tests/test_command_tools_injector.py`
  Satisfies: Resolved Decision 3 (`design.md`) — no spec requirement ID
  (declarative command-tool tier ceiling is a design-phase decision, not a
  numbered R1-R4 requirement). <!-- sdd-owner: implementation -->

- [x] **PR2-T6 — PR2 closing: docs + full verification.**
  Update the module docstring/comment at `registry.py`'s removed prefix
  constants site to point at `agents_system.permissions.evaluate_tool_spec`
  instead (one short note, not a rewrite of the `Tier` docstring itself,
  which stays accurate). Cross-check
  `docs/architecture/permission-model.md` for any explicit prefix-based
  wording it does not currently have (confirmed by direct read during the
  tasks phase: the doc already describes tier-based revalidation only, not
  prefix inference — no edit is expected here, but re-verify at apply time
  and edit only if this PR's diff introduces a mismatch).
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check`,
  `.venv/bin/ruff format --check`. Confirm `git diff --stat` against the
  PR2 branch base; flag in the PR description if it exceeds ~400 lines
  rather than silently merging oversized (see Review Workload Forecast).
  <!-- sdd-owner: implementation -->

---

## PR3 — Grants, Layer-2 grant ceiling (issue #38), `main.py`

Branch: `fix/38-permission-grant-ceiling`
Scope: `config.py`, `harness/factory.py`, `harness/interceptor.py`,
`agent/graph.py`, `main.py`, `src/agents_system/__init__.py` (docstring
only). Depends on PR1 and PR2. Estimated lines: ~340 (design.md Testing
Strategy table).

- [x] **PR3-T1 — `Settings.deploy_grants` from `DEPLOY_GRANTS` env var.**
  RED: add tests to `tests/test_config.py` asserting `Settings` exposes a
  `deploy_grants: dict[str, tuple[str, ...]]` field, that setting the
  `DEPLOY_GRANTS` environment variable to a JSON object (e.g.
  `{"whatsapp__sales-agent": ["read:catalog", "write:orders"]}`) populates
  it via pydantic-settings' existing JSON-env decoding, and that an unset
  `DEPLOY_GRANTS` yields an empty `deploy_grants` (no exception at
  `Settings` construction time — the boot-time failure for a *specific*
  missing model_id is PR3-T3's concern, not this field's own validation).
  GREEN: add the `deploy_grants: dict[str, tuple[str, ...]] = {}` field to
  `Settings` (`config.py:18`'s class), using the same declarative pattern
  as `config.py`'s other structured/JSON-decoded fields.
  Verify: `.venv/bin/pytest -q tests/test_config.py`
  Satisfies: Resolved Decision 5 (`design.md`) — no spec requirement ID
  directly (the spec's "Boot failure without an explicit grant" and
  "Accepted grant forms" requirements are exercised by PR3-T2/T3; this task
  is the config-parsing prerequisite). <!-- sdd-owner: implementation -->

- [x] **PR3-T2 — `EquippedRuntime.deploy_grant_ceiling` + accepted grant forms.**
  RED: add tests to `tests/test_harness_factory.py` covering spec
  Requirement "Accepted grant forms": `build_runtime(..., granted_permissions=
  [Write, "send:message"])` (a mixed list of a `Permission` subclass and a
  registered wire-name string) normalizes every entry through the registry
  and stores the resulting `frozenset[type[Permission]]` on the returned
  `EquippedRuntime.deploy_grant_ceiling`; grant-by-class-only and
  grant-by-name-only both produce an identical ceiling for equivalent
  input; the existing `granted_tools` computation
  (`resolve_tool_surface`/`resolve_command_tool_surface`) is unaffected —
  confirm with an existing-behavior regression assertion that a currently
  passing `test_harness_factory.py` scenario still resolves the same tool
  set as before this task.
  GREEN: add `deploy_grant_ceiling: frozenset[type[Permission]] =
  frozenset()` to the `EquippedRuntime` dataclass (`factory.py:76-96`).
  In `build_runtime` (`factory.py:204-271`), after the existing
  `granted = list(granted_permissions)` line, resolve each entry
  (`permission_registry.resolve(p) if isinstance(p, str) else p`) into the
  stored `frozenset`, independent of and in addition to the existing
  `granted_tools` computation (which keeps using the raw string/class
  values it already accepts today — no behavior change to tool-surface
  resolution in this task).
  Verify: `.venv/bin/pytest -q tests/test_harness_factory.py`
  Satisfies: "Accepted grant forms". <!-- sdd-owner: implementation -->

- [x] **PR3-T3 — `main.py`: explicit `DEPLOY_GRANTS` grant, remove AD-5 auto-grant, boot fails loudly.**
  RED: add tests to `tests/test_main.py` covering spec Requirements "No
  automatic grant" and "Boot failure without an explicit grant": boot with
  a configured runtime whose `model_id` has no `DEPLOY_GRANTS` entry raises
  `DefinitionError` naming that model_id/role (matching the existing
  `whatsapp_runtime_id` failure style at `main.py:433-446`), and the
  application does not start; boot with a `DEPLOY_GRANTS` entry covering
  exactly the required model_id succeeds and the resulting
  `EquippedRuntime.deploy_grant_ceiling` matches the configured entry
  (not `definition.permissions`, the role's own full declared set — assert
  these two are allowed to differ, proving the auto-grant is gone).
  GREEN: in `main.py` (`:357-360`), replace
  `granted_permissions=definition.permissions` with a lookup into
  `settings.deploy_grants[model_id]`, raising `DefinitionError` (same style
  as `main.py:433-446`) before calling `build_runtime` when `model_id not
  in settings.deploy_grants`. Remove the now-dead comment at
  `main.py:300` ("the role's own resolved permissions become
  granted_permissions") and replace it with a short comment describing the
  explicit-grant lookup.
  Verify: `.venv/bin/pytest -q tests/test_main.py`
  Satisfies: "No automatic grant", "Boot failure without an explicit grant".
  <!-- sdd-owner: implementation -->

- [x] **PR3-T4 — Layer-2 revalidates against the persisted ceiling (issue #38 fix + regression).**
  RED: create `tests/test_issue_38_regression.py` with the spec's exact
  scenarios under Requirement "Layer-2 revalidates against the persisted
  deploy grant ceiling (issue #38)": a role declares `Write` among its
  permissions, but the deploy-time grant equips only `Read`-family
  permissions; a T2 tool requiring `Write` reaching Layer-2 revalidation
  mid-turn MUST be denied, because the persisted ceiling — not the role's
  full permission tuple — bounds revalidation; a grant-covered tool (both
  role and deploy grant include `Write`) passes Layer-2. Also rewrite the
  affected assertions in `tests/test_harness_interceptor.py` (currently
  driven by `effective = set(current_permissions)` at `interceptor.py:140`)
  to reflect ceiling-intersected revalidation, so no pre-existing case in
  that file silently regresses under the new logic. Include the PR body
  text `Closes #38` in this PR's description (recorded here so `verify` can
  trace it; the actual PR is opened at delivery time, not by this task).
  GREEN: update `interceptor.intercept` (`interceptor.py:140-141`) to
  intersect `current_permissions` against `equipped.deploy_grant_ceiling`
  (resolved to classes, same R3 issubclass+tier coverage logic as PR2-T3's
  `_deny_reason`, not a plain set-intersection of strings) before the
  `spec.required_permissions` coverage check. Update
  `AgentRuntime.run_turn`'s default (`graph.py:457-459`, currently
  `effective_permissions = permissions if permissions is not None else
  self.permissions`) to fall back to the equipped runtime's
  `deploy_grant_ceiling` instead of `self.permissions` (the role's full
  declared tuple) when no explicit `permissions` override is passed to a
  turn.
  Verify: `.venv/bin/pytest -q tests/test_issue_38_regression.py
  tests/test_harness_interceptor.py tests/test_agent_runtime.py`
  Satisfies: "Layer-2 revalidates against the persisted deploy grant
  ceiling (issue #38)". Closes #38. <!-- sdd-owner: implementation -->

- [x] **PR3-T5 — PR3 closing: docstring update + docs + full verification.**
  Update `src/agents_system/__init__.py`'s module docstring code example
  (currently showing `build_runtime(..., granted_permissions=
  ["read:catalog"])`) to make explicit that this call is now the *only*
  path to a grant — no boot-time auto-grant exists anywhere in the library
  path either (the docstring's own flow was already explicit; add one
  sentence clarifying that `main.py`'s deployment path now requires the
  equivalent explicit `DEPLOY_GRANTS` configuration, cross-referencing
  `docs/architecture/adr-003-permission-hierarchy.md` — written in PR4 —
  so do not create a dangling forward reference; phrase it as "see the
  permission-model ADR" without assuming the exact filename if PR4 has not
  landed yet in the same working tree).
  Verify: `.venv/bin/pytest -q` (full suite, run `tests/test_public_api.py`
  explicitly since it asserts the exact `_EXPORTS` reachability this
  docstring documents), `.venv/bin/ruff check`, `.venv/bin/ruff format
  --check`. Confirm `git diff --stat` against the PR3 branch base; flag if
  it exceeds ~400 lines. <!-- sdd-owner: implementation -->

---

## PR4 — Docs + ADR-003

Branch: `docs/permission-model-4-adr-003`
Scope: `docs/` only — no source or test changes. Depends on PR1-PR3 having
landed (ADR-003 documents shipped, not proposed, behavior). Estimated
lines: ~150 (design.md Testing Strategy table).

- [ ] **PR4-T1 — Write `docs/architecture/adr-003-permission-hierarchy.md`.**
  New ADR, following this repo's existing ADR structure (see
  `adr-001-runtime-topology.md` / `adr-002-agent-model-and-capabilities.md`
  for section conventions: Summary, Context, Decision, Corrections if any).
  Content: the `Permission`/`Tier` class model; R1 (subclass tier
  monotonicity); R2a/R2b (`ToolSpec` ceiling and floor); R3 (grant covers
  descendant only when tier does not exceed); R4 (`untrusted_input` holds
  no T3 permission, any name); the `PermissionRegistry` (explicit
  registration, no string inference, thread-safety); the five Resolved
  Decisions from `design.md` (`ReadFiles` escalation, `Spawn` family,
  command-tool T2 ceiling, evals out-of-scope follow-up, `DEPLOY_GRANTS`
  grant source); the issue #38 fix (persisted deploy grant ceiling). State
  explicitly which ADR-002 sections it amends: C.10 (capability tiers — tier
  values unchanged, only classification mechanism moves from prefix to
  class), C.11 (`untrusted_input` invariant — generalized from `exec:*`
  string matching to any T3-tier class), C.12 (declarative `command_tools`
  — tier ceiling narrowed to T2 only), and the AD-5 auto-grant design
  decision (`adr-002-agent-model-and-capabilities.md:153`, removed).
  Verify: no test command applies (docs-only); confirm the new file exists
  at `docs/architecture/adr-003-permission-hierarchy.md` and every R1-R4 /
  Resolved-Decision item above is present as a heading or named section.
  <!-- sdd-owner: implementation -->

- [ ] **PR4-T2 — Amend ADR-002's superseded sections.**
  Add a short "Superseded by ADR-003" pointer note at the top of each of
  C.10 (`adr-002-agent-model-and-capabilities.md:652`), C.11 (`:712`), and
  C.12 (`:800`), and at the AD-5 auto-grant design decision
  (`:153`) — one or two sentences each, linking to
  `adr-003-permission-hierarchy.md`, without deleting or rewriting the
  original section text (the original text remains the historical record
  of what C.10/C.11/C.12/AD-5 originally decided; ADR-003 is what actually
  ships). Do not alter any other part of `adr-002-...md`.
  Verify: no test command applies (docs-only); confirm exactly four
  pointer notes were added and no other line in the file changed
  (`git diff docs/architecture/adr-002-agent-model-and-capabilities.md`
  should show only small additive hunks at the four cited anchors).
  <!-- sdd-owner: implementation -->

- [ ] **PR4-T3 — Cross-check `docs/architecture/permission-model.md`.**
  Re-read the file in full against the shipped PR1-PR3 behavior. As
  confirmed during this tasks phase, the file already describes
  tier-based revalidation only (no `exec:`/`write:`/`send:` prefix
  inference language) and requires no edit for R1-R4/the registry — verify
  this is still true after PR1-PR3 landed (a scope drift during apply could
  invalidate the earlier read) and add a one-line cross-reference to
  `adr-003-permission-hierarchy.md` in its own "Cross-references" section
  only if none of the existing cross-references already cover it. Do not
  add unrelated edits to this file.
  Verify: no test command applies (docs-only); confirm the file's
  "Cross-references" section lists `adr-003-permission-hierarchy.md`.
  <!-- sdd-owner: implementation -->

- [ ] **PR4-T4 — PR4 closing: state.yaml + final consistency pass.**
  Update `openspec/changes/permission-model/state.yaml`: `phase: archive`
  is NOT set by this task (archive happens separately, at merge, per
  `AGENTS.md`'s SDD-flow mapping — `Closes #NN`-style issue linkage is a
  PR-level concern, not this file's). Only confirm `artifacts.tasks: true`
  and `tasks_progress` are current. Read through all PR1-PR4 tasks above
  once more and confirm every spec requirement ID (R1, R2a, R2b, R3, R4,
  and the eleven other named requirements in `spec.md`) is referenced by at
  least one task's "Satisfies:" line; note any gap found as a new follow-up
  task rather than silently leaving it uncovered. No `CHANGELOG` file
  exists in this repo — do not create one.
  Verify: `.venv/bin/ruff check` and `.venv/bin/ruff format --check` (guard
  against any accidental source edit having crept into a docs-only branch);
  confirm `git diff --stat` for PR4 touches only files under `docs/` and
  `openspec/changes/permission-model/state.yaml`, at or below ~150 lines.
  <!-- sdd-owner: implementation -->

---

## Follow-ups (not part of this change)

- File a GitHub issue for evals' implicit full-grant default
  (`evals/schema.py:56-65`, `evals/runner.py:347-383`) — Resolved Decision
  4 keeps it explicitly out of scope for `permission-model`. Not created by
  this tasks phase; left for the user/orchestrator to file, labeled
  independently of this change's four PRs.
