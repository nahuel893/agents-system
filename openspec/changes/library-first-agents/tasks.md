# Tasks: Library-first custom agents (ADR-004)

Source artifacts: `proposal.md`, `specs/agent-definition-locator/spec.md`,
`specs/agent-registration-serving/spec.md`,
`specs/predefined-agent-governance/spec.md`, `design.md` (Architecture
Decisions D1-D8, File Changes, Testing Strategy Per PR, Threat Matrix — see
`design.md`'s own PR1/PR4a split recommendations, honored below).

Test runner: `.venv/bin/pytest -q`. Lint: `.venv/bin/ruff check .`. Format:
`.venv/bin/ruff format --check .`. Type check: `.venv/bin/mypy src/`. Every
task's verification command MUST be run from the repo root before the task
is checked off; every PR's closing task MUST run all four before that PR is
considered mergeable.

All `codegraph_explore` line references below were re-confirmed live against
this worktree (checked out from `origin/main`) during this tasks phase — the
proposal's Risk table flag about a stale main-checkout CodeGraph
index no longer applies; the index used for this phase matched direct-read
ground truth exactly (`_extends_target` at `loader.py:869-876`,
`_load_role_files` at `:913+`, `_resolve_role_chain` at `:1135+`,
`load_generic` at `:1185`, `resolve` at `:1843`, `to_model_id`/`parse_model_id`
at `openai_adapter.py:54-74`, the inline parser and `DEPLOY_GRANTS` check at
`main.py:280-281`/`:350-359`, `create_app` at `main.py:581-588`, `_EXPORTS` at
`__init__.py:66-80`, `EXPECTED_ROLE_TOOLS`/`PINNED_ROLES` at
`tests/platform_role_contract.py:79-155`).

**Ground-truth note carried forward from `design.md`'s D8**: the governance
spec's phrasing "establish `CHANGELOG.md` at the repository root (none exists
prior to this change)" is stale — `CHANGELOG.md` already exists in this
worktree (confirmed: the `[0.2.0]` section, and `release-please-config.json`'s
`changelog-path: "CHANGELOG.md"` already points at it). PR6 below documents
and verifies this instead of creating a new file — see PR6-T3.

**`_es` doc-twin note (corrected)**: an earlier draft of this file claimed no
`_es` convention exists — that was wrong. The convention is
**directory-based**, not a filename suffix: `docs/platform_es/`,
`docs/architecture_es/`, `docs/operations_es/`, and `docs/delivery_es/`
mirror `docs/platform/`, `docs/architecture/`, `docs/operations/`, and
`docs/delivery/` file-for-file (confirmed: `fd -t d _es docs` finds all
four directories; e.g. `docs/platform_es/role.md` mirrors
`docs/platform/role.md`, `docs/architecture_es/adr-003-permission-hierarchy.md`
mirrors `docs/architecture/adr-003-permission-hierarchy.md`). Every docs task
below that edits a file under `docs/platform/` or `docs/architecture/`
updates the matching `docs/platform_es/`/`docs/architecture_es/` twin in the
SAME task, in Spanish, matching the existing `_es` files' tone (informal
`vos`-register, as already used throughout `docs/platform_es/*.md` and
`docs/architecture_es/*.md` — confirmed by direct read). `docs/operations_es/`
and `docs/delivery_es/` have no file this change touches, so no task below
edits them.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~2,610 total across 10 PR slices (PR1a ~240, PR1b ~215, PR2 ~350, PR3 ~265, PR4a-i ~165, PR4a-ii ~245, PR4b ~280, PR5 ~240, PR6 ~320, PR7 ~290 — PR3/PR5/PR7 revised up from the original draft to account for `docs/*_es/` twin edits, see the corrected `_es` doc-twin note above) |
| 400-line budget risk | Medium (per-slice; three slices — PR2, PR6, PR7 — sit closest to the ceiling; PR7 rose the most from adding the ADR-004 `_es` twin and the ADR-002 `_es` pointer notes) |
| Chained PRs recommended | Yes |
| Suggested split | PR1a → PR1b → PR2 → PR3 → PR4a-i → PR4a-ii → PR4b → PR5 → PR6 → PR7 |
| Delivery strategy | ask-on-risk |
| Chain strategy | feature-branch-chain (PR1a targets the tracker/feature branch; each later PR targets the immediate previous PR's branch, per `proposal.md`'s own PR Split header) |

```text
Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: Medium
```

`design.md`'s own Testing Strategy table already flagged combined PR1
(~420-480) and combined PR4a (~380-430) as High/Medium-High budget risk. Both
are pre-split below exactly as the orchestrator requested and as
`design.md`'s own "PR1 split recommendation" / "PR4a split recommendation"
sections describe — no PR below is estimated above 400 lines. If any slice's
actual diff approaches 400 lines during apply, split its tests-only portion
into a preceding chained slice rather than widening the budget silently (same
rule the `permission-model` tasks phase used).

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Locator types + disk-reading dispatch (`RoleLocator`/`FolderLocator`/`InlineLocator`, widened `_load_role_files`/`_resolve_role_chain`/`load_generic`/`resolve()`) | PR1a | `.venv/bin/pytest -q tests/test_locator_folder.py tests/test_locator_inline.py` | Full existing predefined-role/deployment-override suite (`.venv/bin/pytest -q tests/platform_role_contract.py tests/test_harness_loader.py`) | `git revert` PR1a alone — no other PR depends on anything but its exported types/signatures |
| 2 | `_extends_target` fail-loud rewrite (D2, all 8 cases + threat-matrix path-traversal/symlink cases) | PR1b | `.venv/bin/pytest -q tests/test_extends_fail_loud.py` | Same predefined-role/deployment-override suite, unchanged output required | `git revert` PR1b alone — reverts to PR1a's still-present (unused) last-segment-stripping behavior |
| 3 | `Agent` Python API (`agent/spec.py`, `__init__.py` export) | PR2 | `.venv/bin/pytest -q tests/test_agent_from_params.py tests/test_agent_from_folder.py` | N/A — pure construction/resolution, no live model/service call | `git revert` PR2 alone — no other PR imports `Agent` internals, only its public shape |
| 4 | Skills for importer agents (D4 precedence + doc fix) | PR3 | `.venv/bin/pytest -q tests/test_skills_precedence.py` | N/A — file-read path, no live service | `git revert` PR3 alone |
| 5 | `create_app` new params (unused by lifespan yet) | PR4a-i | `.venv/bin/pytest -q tests/test_create_app_agents_param.py` | N/A — signature/validation only | `git revert` PR4a-i alone (PR4a-ii has not yet consumed the new params) |
| 6 | `lifespan()` loop rewritten around `agents`/`grants`/`clients` | PR4a-ii | `.venv/bin/pytest -q tests/test_main.py -k registration` | `uv run python -m agents_system.demo` boot smoke-check (env-driven fallback path, unchanged) | `git revert` PR4a-ii — falls back to PR4a-i's params-accepted-but-unused state |
| 7 | Env migration (`AGENT_REGISTRATIONS`, parser deletion) | PR4b | `.venv/bin/pytest -q tests/test_agent_registrations_parsing.py tests/test_main.py` | `uv run python -m agents_system.demo` boot smoke-check with `AGENT_REGISTRATIONS` set | `git revert` PR4b — restores `to_model_id`/`parse_model_id` and the old env semantics |
| 8 | Terminology rename + `examples/` relocation | PR5 | `.venv/bin/pytest -q` (full suite — rename touches identifiers broadly) | `python examples/demo/app.py` manual run (documented, not CI) | `git revert` PR5 — docs/identifiers only, plus one file move |
| 9 | Guard 1 governance (snapshot + contract test + CI marker check) | PR6 | `.venv/bin/pytest -q tests/platform_role_contract.py tests/test_check_role_governance_marker.py` | N/A — CI-only check, exercised by the fixture unit test | `git revert` PR6 — CI-only, no runtime behavior change |
| 10 | ADR-004 + ADR-002 amendments | PR7 | N/A — docs only | N/A | `git revert` PR7 — docs only |

## Cross-PR Ground Rules

- Each PR MUST be independently mergeable: full suite green
  (`.venv/bin/pytest -q`), `.venv/bin/ruff check .` clean, `.venv/bin/ruff
  format --check .` clean, `.venv/bin/mypy src/` clean, before its closing
  task is checked off.
- Strict TDD (RED-GREEN-REFACTOR) for every new/rewritten test in this change,
  same convention `permission-model`'s tasks phase followed
  (`openspec/config.yaml`'s repo-wide `rules.apply.tdd` stays `false`; this
  change follows Strict TDD by its own convention regardless). Always run the
  RED test first and confirm it fails for the stated reason before writing
  the GREEN change.
- `merge()`, the injector (`resolve_tool_surface`/`resolve_command_tool_surface`/
  `_deny_reason`), `_append_base_contract`, and `_validate_untrusted_input_exec`
  (now class-based, from `permission-model`) are **untouched** by every task
  below — they already operate on `RawDefinition`/`AgentDefinition`, agent-shape-
  agnostic. No task in this file edits `harness/injector.py` or
  `harness/registry.py`.
- `load_override` (`loader.py:1251+`) and `merge()` (`loader.py:1653+`) keep
  their exact current subtractive-only deployment-override semantics — no task
  below edits their control flow, only threads the widened `RoleLocator` type
  through call sites that already pass a platform-role string.
- Feature Branch Chain per `proposal.md`'s own PR Split header: PR1a targets
  the tracker/feature branch (`docs/library-first-agents-adr-004` or
  equivalent, per delivery-time naming); PR1b targets PR1a's branch; PR2
  targets PR1b's branch; and so on down the chain in the order listed in the
  Review Workload Forecast table above. If GitHub shows a previous slice's
  changes in a child diff, retarget/rebase before requesting review.
- No task below renames or removes `platform/roles/*/manifest.md`'s wire
  format — PR5's rename touches identifiers, comments, and docs only, never a
  manifest's declared `tools:`/`permissions:`/`extends:` values.
- Every PR flagged breaking in `proposal.md`'s Backward Compatibility &
  Migration section (PR1b's `extends:` fail-loud, PR4a-ii's `create_app`
  registration change, PR4b's runtime-id-scheme removal, PR5's
  `agents_system.demo` module removal) ships with a squash-merge subject
  carrying `!` and a `BREAKING CHANGE:` footer, so release-please's
  `bump-minor-pre-major: true` config records it — each closing task below
  repeats this as an explicit reminder for that PR.

---

## PR1a — Locator types + disk-reading dispatch

Branch: `feat/adr-004-1a-locator-types`
Scope: `src/agents_system/harness/loader.py` only. Platform-role resolution
stays byte-for-byte unchanged in this slice — `_extends_target` is not
rewritten here (that is PR1b). Depends on nothing (first slice in the chain).
Estimated lines: ~240 (design.md Testing Strategy table: PR1a ~220-260).

- [ ] **PR1a-T1 — `RoleLocator`/`FolderLocator`/`InlineLocator` types (D1).**
  RED: create `tests/test_locator_folder.py` and `tests/test_locator_inline.py`
  asserting: `FolderLocator(path=..., root=..., overrides={})` and
  `InlineLocator(raw=..., parent=None)` are frozen dataclasses (mutation
  raises `dataclasses.FrozenInstanceError`); `RoleLocator = str |
  FolderLocator | InlineLocator` is importable from `harness.loader`; a bare
  `str` locator still `isinstance`-matches `RoleLocator` (no wrapping
  required — D1's own rejected-alternative rationale, asserted directly so a
  future refactor cannot silently reintroduce wrapping).
  GREEN: add `FolderLocator`, `InlineLocator`, `RoleLocator` to
  `harness/loader.py` immediately after `RawDefinition` (currently ending
  before line 304's `_split_frontmatter`), exactly per `design.md`'s D1 code
  block (including both classes' full docstrings — `FolderLocator.root`
  bounding the importer-space `extends:` search, `InlineLocator.parent`
  typed `RoleLocator | None` for uniformity though only ever `str | None` in
  practice).
  Verify: `.venv/bin/pytest -q tests/test_locator_folder.py
  tests/test_locator_inline.py`
  Satisfies: "Agent locator discriminates exactly three source kinds".
  <!-- sdd-owner: implementation -->

- [ ] **PR1a-T2 — `_load_role_files` 3-way dispatch (platform / folder / inline).**
  RED: extend `tests/test_locator_folder.py` with: a `FolderLocator` pointing
  at a well-formed importer folder (own `role.md`/`manifest.md`/`policy.md`)
  resolves to a `RawDefinition` with the same field set a platform-role
  folder produces; a `FolderLocator` whose folder is missing `policy.md`
  raises `DefinitionError` naming the missing file and the folder path (same
  class/shape a missing platform file raises); a `FolderLocator` folder
  declaring `abstract: true` in its own `manifest.md` round-trips
  `is_abstract=True` through `_load_role_files`, exactly like the platform
  branch (`test_locator_folder.py::test_abstract_folder_agent_rejected`,
  named here per `design.md`'s Open Questions note — this test proves the
  existing abstract-rejection path already covers a `FolderLocator` leaf with
  no extra code). Extend `tests/test_locator_inline.py` with: an
  `InlineLocator(raw=<RawDefinition>, parent=None)` returns its own `raw`
  unchanged from `_load_role_files` with no disk read at all (assert via a
  folder path that does not exist anywhere on disk, proving no read was
  attempted); confirm the existing platform-role branch
  (`isinstance(locator, str)`) produces byte-identical output to before this
  task for every existing fixture role (regression assertion, not a new
  scenario).
  GREEN: widen `_load_role_files`'s signature from `role_type: str` to
  `locator: RoleLocator`, adding two new branches ahead of the existing body:
  an `InlineLocator` branch that returns `(locator.raw, None, False)`
  immediately (no disk I/O — `is_abstract` is always `False` for an inline
  definition, since abstractness is a folder-manifest-only concept); a
  `FolderLocator` branch that reads `role.md`/`manifest.md`/`policy.md` from
  `locator.path` the same way the platform branch reads from
  `platform_root/roles/<name>`, reusing the exact same `_read_md`/
  `_split_design_notes`/`_parse_command_tools`/`_parse_untrusted_input` calls
  (only the folder path source differs — no new parsing code). The existing
  `isinstance(locator, str)` platform branch is otherwise untouched,
  byte-for-byte. `_extends_target(parent_raw)` calls inside this function
  stay as today's last-segment-stripping call in this slice (PR1b rewrites
  the function itself, not this call site's shape).
  Verify: `.venv/bin/pytest -q tests/test_locator_folder.py
  tests/test_locator_inline.py tests/platform_role_contract.py
  tests/test_harness_loader.py`
  Satisfies: "Importer-folder locator reads the same three-file contract",
  "An inline locator is used" (part of "Agent locator discriminates exactly
  three source kinds"). <!-- sdd-owner: implementation -->

- [ ] **PR1a-T3 — `_resolve_role_chain` locator-keyed cycle detection + chain walk.**
  RED: extend `tests/test_locator_inline.py` with a cycle scenario built from
  a chain of `InlineLocator`s whose `parent` values eventually repeat (an
  inline locator referencing itself indirectly through a `str` platform name
  that in turn — via a test-only monkeypatched platform folder — points back)
  and assert `DefinitionError` naming the cycle, keyed by the new
  `_locator_key(locator)` shape (`f"platform:{name}"` /
  `f"folder:{path.resolve()}"` / `f"inline:{id(raw)}"`) rather than the old
  bare-string `seen` list; assert the existing `_MAX_ROLE_CHAIN_DEPTH` (8)
  ceiling still fires identically for a locator-based chain of the same
  depth. Confirm (regression) that a normal platform-role chain (e.g.
  `sales-agent` → `agent`) still resolves and folds identically to before
  this task.
  GREEN: rewrite `_resolve_role_chain`'s `seen: list[str]` /
  `current: str | None` walk to operate over `RoleLocator` values, adding a
  private `_locator_key(locator: RoleLocator) -> str` helper (the three
  f-string forms above) used both for the `seen` membership check and for
  the cycle-message rendering (render each `chain` entry's locator key, not
  a bare role-type string, when the cycle spans folder/inline locators).
  `_load_role_files(current, roots)` becomes `_load_role_files(current_locator,
  roots)`; the depth-check and fold-order logic (`chain[-1]` root-first fold
  via `_fold_parent_into_child`) are unchanged.
  Verify: `.venv/bin/pytest -q tests/test_locator_inline.py
  tests/test_harness_loader.py tests/test_harness_loader_edge.py`
  Satisfies: "Agent locator discriminates exactly three source kinds" (the
  shared-pipeline half of that requirement — every locator kind flows through
  the same resolution/merge/injection code path). <!-- sdd-owner: implementation -->

- [ ] **PR1a-T4 — `load_generic`/`resolve()` signature widening.**
  RED: extend `tests/platform_role_contract.py`'s existing regression
  coverage (or add `tests/test_locator_folder.py::test_resolve_folder_locator_end_to_end`)
  asserting `resolve(FolderLocator(...), client=None, roots=roots)` produces
  a complete `AgentDefinition` (base contract appended, tiers/untrusted_input
  validated) for a well-formed importer folder with no `extends:`; assert
  `resolve(FolderLocator(...), client="acme", roots=roots)` raises
  `DefinitionError` (Q5 from `design.md`: `client` is only valid with a `str`
  locator — enforced here even though PR2/PR3 are what actually exercise this
  path end-to-end, since `resolve()`'s own signature is what this task
  widens). Run the FULL existing predefined-role/deployment-override
  regression suite and confirm every test passes unchanged (spec's own
  "Predefined-role resolution is behavior-unchanged" requirement, exercised
  directly).
  GREEN: widen `load_generic(role_type: str, roots)` and
  `resolve(role_type: str, *, client=None, roots=None)`
  (`loader.py:1843+`) to accept `locator: RoleLocator` in place of
  `role_type: str` (parameter renamed at the same time — every existing
  caller passing a positional/keyword `role_type=` string keeps working
  unchanged, since `str` is one of `RoleLocator`'s members). Add the `client`
  + non-`str`-locator guard: `if client is not None and not
  isinstance(locator, str): raise DefinitionError(...)` inside `resolve()`,
  before any resolution work begins. No other line in either function
  changes.
  Verify: `.venv/bin/pytest -q` (full suite — this task's signature widening
  is the one most likely to have a caller this session missed; run
  everything, not just the locator test files)
  Satisfies: "Predefined-role resolution is behavior-unchanged", "All eight
  predefined roles resolve unchanged", "A deployment override still narrows
  only", "No implicit client-override root is assumed for a registered
  agent" (the `client`-requires-`str`-locator half). <!-- sdd-owner: implementation -->

- [ ] **PR1a-T5 — PR1a closing: docs + full verification.**
  Add module-level docstring notes to `FolderLocator`/`InlineLocator`/
  `RoleLocator` (already drafted in PR1a-T1) confirming they cross-reference
  `design.md`'s D1; no other doc file changes in this slice (doc updates for
  the skills contradiction land in PR3, ADR-004 lands in PR7).
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Run `git diff
  --stat` against the tracker/feature branch base and confirm the total is
  at or below ~260 lines; if it exceeds 400, split the newest task's tests
  into a preceding slice rather than merging oversized.
  <!-- sdd-owner: implementation -->

---

## PR1b — `_extends_target` fail-loud rewrite

Branch: `fix/adr-004-1b-extends-fail-loud`
Scope: `src/agents_system/harness/loader.py` only (`_extends_target` itself,
plus its two new helpers `_resolve_within_root`/`_importer_root_of`/
`_platform_role_name_or_none`). Depends on PR1a (`RoleLocator`/
`FolderLocator`/`InlineLocator` and the locator-keyed chain walk must exist
before `_extends_target` can return a `RoleLocator` instead of a bare `str`).
Estimated lines: ~215 (design.md Testing Strategy table: PR1b ~200-230).
This is the actual bug fix `proposal.md`'s Scope item 3 names
(`_extends_target`'s silent last-segment stripping,
`harness/loader.py:869-876`) and the change's one genuine new trust boundary
per `design.md`'s Threat Matrix — an importer-supplied folder path and its
`extends:` chain.

- [ ] **PR1b-T1 — `_extends_target` fail-loud rewrite: all 8 enumerated cases + threat-matrix path/symlink cases.**
  RED: create `tests/test_extends_fail_loud.py` covering spec Requirement
  "`extends:` fails loudly when unplaceable in either locator space" and
  every case in `design.md`'s D2 enumerated table: (1) an empty/whitespace-only
  `extends:` value raises `DefinitionError`; (2) a bare name or
  `platform/roles/<name>` form whose folder is missing raises immediately,
  with **no** importer-space fallback attempted (the deliberately-stricter
  case D2 calls out — assert no importer folder of the same name is ever
  consulted); (3) a multi-segment value, not platform-prefixed, when
  `current` has no importer root at all (a pure-Python
  `Agent(extends="some/path")` case) raises `DefinitionError`; (4) an
  ABSOLUTE multi-segment value is rejected BEFORE any filesystem check — the
  only syntactic (pre-resolution) rejection
  (`tests/test_extends_fail_loud.py::test_absolute_path_rejected` — one of
  the three threat-matrix RED tests named verbatim in `design.md`'s Threat
  Matrix table); (5) containment is checked AFTER resolution, never by
  rejecting a `..` token on sight (`design.md`'s D2 Resolved Decision, this
  tasks phase) — cover all three resulting sub-cases explicitly:
  (5a) a RELATIVE value containing `..` whose fully resolved path (following
  symlinks) stays INSIDE `root` (a sibling folder, e.g. `../base-support`
  from `<root>/vip-support/` when `<root>/base-support/` exists) is
  **ACCEPTED**, resolving to that sibling `FolderLocator`
  (`::test_dotdot_sibling_accepted` — this is the positive case D2's
  Resolved Decision exists to make possible, and PR1b-T2 below is where its
  full `extends:`-resolution consequence is exercised end-to-end); (5b) a
  RELATIVE value containing `..` whose fully resolved path escapes `root`
  (e.g. `../../etc` from the same folder) is **REJECTED** after resolution
  (`::test_dotdot_escape_rejected` — renamed from an earlier draft's
  `test_dotdot_segment_rejected`, which incorrectly rejected every `..`
  syntactically); (5c) a value resolving syntactically inside `root` but
  whose symlink-resolved real path escapes it is rejected with the SAME
  message shape as 5b, after resolution
  (`::test_symlink_escape_rejected` — the third threat-matrix RED test named
  in `design.md`); (6) a value resolving inside `root` whose target folder is
  missing or lacks one of the three required files raises the SAME
  `DefinitionError` shape `_load_role_files` already raises for a missing
  platform folder; (7) a cycle in the walked chain still raises
  `DefinitionError` via PR1a-T3's `_locator_key`-based `seen` check
  (regression assertion — this case's mechanism did not change in this
  task, only what feeds it); (8) a chain deeper than
  `_MAX_ROLE_CHAIN_DEPTH` (8) still raises (regression assertion, same
  reason). Add the two named regression scenarios from spec: "A path that
  matches no importer file and no platform role fails loudly" (naming the
  exact unresolved value, e.g. `"some/nonexistent/path/custom-agent"` — MUST
  NOT silently collapse to `"custom-agent"` and search `platform_root/roles`
  for it) and "A last-segment collision with a real predefined role no
  longer silently extends the wrong parent" (the exact regression this
  requirement exists to close — an importer manifest declaring `extends:
  some/importer/path/agent`, where `agent` also happens to be the generic
  agent's platform-role name, and `some/importer/path/agent` does not exist
  under the importer's own root, MUST raise naming the full unresolved
  value, MUST NOT silently extend `platform/roles/agent`); and "A value that
  resolves unambiguously in exactly one space still succeeds" (the
  happy-path regression guard).
  GREEN: replace `_extends_target(raw: Any) -> str`
  (`loader.py:869-876`, today's `return
  str(raw).strip().rstrip("/").rsplit("/", 1)[-1]` unconditional
  last-segment-stripping bug) with the full D2 algorithm and its exact
  signature: `_extends_target(raw: Any, *, current: RoleLocator, roots:
  RootConfig) -> RoleLocator`, plus `_resolve_within_root(root: pathlib.Path,
  relative: str) -> pathlib.Path | None` (rejects ONLY an absolute value
  before any filesystem access; a relative value — including one containing
  `..` — is resolved relative to `root` and fully resolved via
  `Path.resolve()`, following symlinks, and accepted only when that final
  path lands inside `root`; returns `None` — never raises — for anything
  whose fully resolved path does not land inside `root`, whether via a `..`
  escape or a symlink resolving outside it, so every "cannot place this
  extends: value" case raises exactly ONE `DefinitionError` from the
  caller — see `design.md`'s D2 Resolved Decision for the full rationale),
  `_importer_root_of(locator:
  RoleLocator) -> pathlib.Path | None` (returns `locator.root` for a
  `FolderLocator`, `None` for `str`/`InlineLocator`), and
  `_platform_role_name_or_none(value: str) -> str | None` (accepts exactly a
  bare single segment matching `_SAFE_SEGMENT`, or a value whose entire path
  is `platform/roles/<single segment>` — anything else, e.g. a multi-segment
  value not prefixed `platform/roles/`, is never treated as a platform-role
  reference). Update every call site inside `_load_role_files`/
  `_resolve_role_chain` that currently calls `_extends_target(parent_raw)`
  (unkeyed, `str`-returning) to the new keyword-argument, `RoleLocator`-returning
  form, passing `current=<the locator currently being resolved>` and
  `roots=roots`.
  Verify: `.venv/bin/pytest -q tests/test_extends_fail_loud.py`
  Satisfies: "`extends:` fails loudly when unplaceable in either locator
  space", "A path that matches no importer file and no platform role fails
  loudly", "A last-segment collision with a real predefined role no longer
  silently extends the wrong parent", "A value that resolves unambiguously
  in exactly one space still succeeds". <!-- sdd-owner: implementation -->

- [ ] **PR1b-T2 — `extends:` resolves generic-or-predefined and importer-relative parents (folder-integration level).**
  RED: extend `tests/test_extends_fail_loud.py` (or a sibling
  `tests/test_extends_resolution.py`) covering spec Requirements
  "`extends:` resolves a generic-or-predefined parent unchanged" and
  "`extends:` resolves an importer-relative parent", exercised through real
  on-disk manifest folders resolved end-to-end (distinct from PR1b-T1's
  direct/unit-level calls into `_resolve_within_root`/`_extends_target` —
  this task proves the same D2 Resolved Decision holds through the FULL
  chain-walk, not just the helper function in isolation): an importer
  folder's `manifest.md` declaring `extends: agent` (bare name) resolves the
  parent to `platform/roles/agent`, identically to a predefined role
  declaring the same value; an importer folder declaring `extends:
  platform/roles/sales-agent` (path form) resolves the parent to the
  predefined `sales-agent` role, and the importer agent's manifest folds on
  top of it using the same additive-inheritance rules a predefined-role
  child uses; an inline Python-built definition (`InlineLocator`) declaring
  `extends` pointing at an importer folder path resolves that parent from
  the importer folder, not from `platform_root`. Per `design.md`'s D2
  Resolved Decision (recorded this tasks phase — containment is checked
  AFTER resolution, never by rejecting a `..` token on sight), cover all four
  of its concrete cases at this folder-integration level:
  (a) **sibling via `..` accepted** — two importer folders under the
  importer's own root (`agents/base-support/`, `agents/vip-support/`), where
  `vip-support/manifest.md` declares `extends: ../base-support`, resolve
  `vip-support`'s parent to `agents/base-support` under the importer's own
  root, and the child's manifest folds on top (spec's own "An importer agent
  extends a sibling importer agent" scenario);
  (b) **escape via `..` rejected** — a sibling folder's manifest declaring
  `extends: ../../etc` (climbing past the importer agents root passed via
  `RootConfig`) raises `DefinitionError` once the fully resolved path lands
  outside that root;
  (c) **escape via symlink rejected** — a folder inside the importer root
  containing a symlink that resolves outside it, referenced by `extends:`,
  raises `DefinitionError` with the same message shape as (b) once resolved;
  (d) **absolute rejected** — `extends: /etc/passwd` (or any absolute value)
  raises `DefinitionError` before any filesystem access is attempted, same
  as PR1b-T1's unit-level case.
  GREEN: no new production code expected beyond PR1b-T1's
  `_extends_target`/`_resolve_within_root`/`_platform_role_name_or_none`
  rewrite — this task proves that rewrite holds through the full chain-walk
  (`_resolve_role_chain` calling `_extends_target` repeatedly as it folds an
  importer manifest's ancestors), not just via a direct unit call. If any RED
  scenario above fails for a reason other than "not yet exercised at the
  folder-integration level," treat it as a genuine gap in PR1b-T1's
  implementation and fix it there, not by adding parallel logic in this task.
  Verify: `.venv/bin/pytest -q tests/test_extends_fail_loud.py` (or the
  sibling file's own path, per whichever name was used above)
  Satisfies: "`extends:` resolves a generic-or-predefined parent unchanged",
  "An importer agent extends the generic agent by bare name", "An importer
  agent extends a predefined role by path form", "`extends:` resolves an
  importer-relative parent", "An importer agent extends a sibling importer
  agent", "An inline definition extends an importer-folder agent",
  "`extends:` fails loudly when unplaceable in either locator space" (the
  folder-integration half of the escape/absolute cases already unit-tested
  in PR1b-T1). <!-- sdd-owner: implementation -->

- [ ] **PR1b-T3 — PR1b closing: full regression + docs + BREAKING CHANGE marker.**
  Run the FULL existing predefined-role/deployment-override regression suite
  and confirm every test still passes unchanged (this is the safety gate
  `proposal.md`'s own Risks table names for the locator-generalization PRs —
  "the full existing predefined-role/deployment-override test suite is the
  regression gate before any Agent API PR lands"). Add a short docstring
  note to `_extends_target` cross-referencing `design.md`'s D2 and this
  requirement's spec scenarios.
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR1b's branch base; flag if it exceeds ~400 lines. **This
  PR's squash-merge subject MUST carry `!` and a `BREAKING CHANGE:` footer**
  (per `proposal.md`'s Backward Compatibility & Migration item 7 — "`extends:`
  now fails loudly where it used to silently collapse to a same-named
  platform role via last-segment stripping," explicitly framed as "a
  deliberate safety-positive break") so release-please records it.
  <!-- sdd-owner: implementation -->

---

## PR2 — `Agent` Python API

Branch: `feat/adr-004-2-agent-api`
Scope: new `src/agents_system/agent/spec.py`; `src/agents_system/__init__.py`
(`_EXPORTS` entry); `tests/test_public_api.py` (`_EXPECTED_EXPORTS` entry).
Depends on PR1a + PR1b (`Agent._to_locator()` produces `FolderLocator`/
`InlineLocator`, and `extends:` resolution must already fail loudly before an
`Agent`-authored chain can rely on it). Estimated lines: ~350 (design.md
Testing Strategy table: ~330-370).

- [ ] **PR2-T1 — `Agent` frozen dataclass core + `_to_locator()` (pure-Python branch).**
  RED: create `tests/test_agent_from_params.py` covering spec Requirement
  "`Agent(...)` Python-parameter construction resolves without disk":
  `Agent(name="triage-bot", extends="agent", tools=["catalog_search"],
  permissions=["read:catalog"])` resolves through `resolve(agent._to_locator(),
  roots=roots)` to a complete `AgentDefinition` with zero files read for the
  agent's own content (assert via a `tools`/`permissions` fixture registry
  with no folder anywhere on disk for `"triage-bot"`); the resolved
  definition folds the declared fields onto the `extends: agent` parent
  (base prompt contract, T3 sandbox gate, `untrusted_input` handling — spec's
  "A pure-Python agent still receives every library invariant" scenario,
  asserted directly: construct a T3-tier-permission variant and confirm the
  same `_validate_untrusted_input_exec` rejection fires as it would for a
  predefined role); `Agent.skill_contents` naming a skill not listed in
  `skills` raises `DefinitionError` at `Agent.__init__` time (the
  `__post_init__` validation), before any resolution is attempted.
  GREEN: create `src/agents_system/agent/spec.py` with the `Agent` frozen
  dataclass exactly per `design.md`'s D3 code block (`name`, `extends: str |
  Agent = "platform/roles/agent"`, `tools`/`permissions`/`skills` tuples,
  `skill_contents`/`context`/`escalation_rules`/`delegation_policy`/
  `memory_policy`/`audit_policy` mappings, `execution_limits`,
  `untrusted_input`, `system_prompt`, `version`, `_folder`/
  `_folder_overrides` repr-hidden fields), its `__post_init__` validation,
  and `_to_locator()`'s pure-Python (`InlineLocator`) branch only in this
  task (the `_folder is not None` branch is PR2-T2's concern). `parent:
  RoleLocator | None` resolves `extends` per D3's rule: `isinstance(self.extends,
  Agent)` → eager `self.extends._to_locator()`; otherwise the bare `str`
  passed through unresolved (lazy, walked by PR1b's `_extends_target` at
  chain-walk time).
  Verify: `.venv/bin/pytest -q tests/test_agent_from_params.py`
  Satisfies: "`Agent(...)` Python-parameter construction resolves without
  disk", "A pure-Python agent resolves with no folder", "A pure-Python agent
  still receives every library invariant". <!-- sdd-owner: implementation -->

- [ ] **PR2-T2 — `Agent.from_folder` + folder/params compose (override precedence).**
  RED: create `tests/test_agent_from_folder.py` and
  `tests/test_agent_folder_plus_overrides.py` covering spec Requirements
  "`Agent.from_folder` produces the loader's `AgentDefinition` shape" and
  "Folder content and Python parameters compose, with parameters as the
  override layer": `Agent.from_folder(path)` with no overrides produces the
  identical `AgentDefinition` field set/types a predefined-role folder
  produces (structurally equivalent fixture folders, differing only in
  role-specific content); `Agent.from_folder(path)` performs **zero**
  filesystem access at construction time (assert via a `path` that does not
  exist yet at `Agent.from_folder()` call time but does exist by the time
  `resolve()` actually reads it — proving the "validate at first use" laziness
  D3 documents); `Agent.from_folder(path, tools=["catalog_search",
  "order_writer"])` where the folder's own `manifest.md` declares `tools:
  [catalog_search]` resolves to the effective `tools=["catalog_search",
  "order_writer"]` (field REPLACED, not merged — D3's explicit non-merge
  choice); the same call's `permissions` (not passed as an override) keeps
  exactly the folder's declared value; `Agent.from_folder(path,
  bogus_field=...)` raises `DefinitionError` naming the unknown override, per
  `__post_init__`'s `_AGENT_OVERRIDABLE_FIELDS` check.
  GREEN: add `Agent.from_folder(cls, path, /, **overrides)` (classmethod,
  builds `_folder`/`_folder_overrides`, no I/O) and `_to_locator()`'s
  `FolderLocator` branch (`self._folder is not None` →
  `FolderLocator(path=self._folder, root=self._folder.parent,
  overrides=self._folder_overrides)`) exactly per `design.md`'s D3 code
  block. Wire `FolderLocator.overrides` consumption into PR1a-T2's
  `_load_role_files` `FolderLocator` branch: after reading
  `role.md`/`manifest.md`/`policy.md` into a `RawDefinition`, apply each
  key in `locator.overrides` as a field replacement on that `RawDefinition`
  before returning it (this is the one small addition to PR1a-T2's branch
  this task makes — `FolderLocator.overrides` existed as a field since PR1a
  but was unconsumed until now). Define `_AGENT_OVERRIDABLE_FIELDS` as the
  set of `Agent`'s own dataclass field names minus `_folder`/
  `_folder_overrides` themselves.
  Verify: `.venv/bin/pytest -q tests/test_agent_from_folder.py
  tests/test_agent_folder_plus_overrides.py tests/test_locator_folder.py`
  Satisfies: "`Agent.from_folder` produces the loader's `AgentDefinition`
  shape", "from_folder output shape matches a predefined role's", "Folder
  content and Python parameters compose, with parameters as the override
  layer", "An explicit parameter overrides the same folder-declared field",
  "A field not passed as a parameter keeps the folder's value".
  <!-- sdd-owner: implementation -->

- [ ] **PR2-T3 — `extends: str | Agent` (eager Agent-to-Agent, lazy str) + additive inheritance (Option B).**
  RED: create `tests/test_agent_extends_agent.py` and
  `tests/test_agent_extends_predefined_role.py` covering spec Requirements
  "Additive inheritance from the generic agent or any predefined role (Option
  B)" and the `extends: str | Agent` half of D3: `Agent(name="vip-support",
  extends=<another already-constructed Agent instance>, ...)` resolves the
  parent object identity at `Agent.__init__` time with **zero** I/O
  (`_to_locator()` calls `self.extends._to_locator()` directly — assert this
  eager resolution never touches disk even for a folder-backed parent
  `Agent`, since `_to_locator()` itself is pure Python for the parent too, up
  to the point PR1a's `resolve()` actually reads a `FolderLocator`'s path);
  an `Agent` extending `platform/roles/sales-agent` by bare-string `extends`
  inherits `sales-agent`'s full tool surface plus its own added tool (Option
  B additive-inheritance assertion, reusing `EXPECTED_ROLE_TOOLS["sales-agent"]`
  from `tests/platform_role_contract.py` as the expected floor); an `Agent`
  extending `"agent"` (the generic agent, no predefined-role ancestor)
  inherits only the generic floor (`session_state`/`escalation_notifier`
  tools, `read:session`/`send:escalation` permissions, `supervised` autonomy
  floor) before its own fields fold on top. Confirm a mutable-attempt on an
  already-constructed `Agent` used as another's `extends=` raises
  `dataclasses.FrozenInstanceError` (the immutability argument D3 makes for
  why cycle-free-by-construction holds).
  GREEN: no new production code beyond what PR2-T1/PR2-T2 already shipped —
  `Agent`'s `extends: str | Agent` field and `_to_locator()`'s branch on
  `isinstance(self.extends, Agent)` are already in place; this task is
  test-only, proving the eager/lazy split and Option B fold behavior that
  PR1a-T3's chain walk + the existing (unchanged) `_fold_parent_into_child`
  already provide "for free." If any of the RED scenarios above fails for a
  reason other than "test file doesn't exist yet," treat it as a genuine gap
  in PR2-T1/PR2-T2 and fix the narrowest production code needed, not this
  task's test file.
  Verify: `.venv/bin/pytest -q tests/test_agent_extends_agent.py
  tests/test_agent_extends_predefined_role.py`
  Satisfies: "Additive inheritance from the generic agent or any predefined
  role (Option B)", "An importer agent extending sales-agent inherits its
  tool surface", "An importer agent extending the generic agent starts from
  the minimal floor". <!-- sdd-owner: implementation -->

- [ ] **PR2-T4 — Public export: `__init__.py` + `test_public_api.py`.**
  RED: add a `test_agent_export` parametrize case to `tests/test_public_api.py`
  (matching that file's own existing per-export pattern, `_EXPECTED_EXPORTS`
  dict at `:26-40`) asserting `agents_system.Agent` resolves lazily to
  `agent.spec.Agent`, and that `import agents_system; agents_system.Agent`
  does **not** import `agents_system.agent.graph` (reusing
  `test_import_agents_system_does_not_import_agent_graph`,
  `tests/test_public_api.py:150-158`, extended to cover this new export
  too — `agent/spec.py` only imports from `harness.loader`, never
  `agent.graph`, per D6).
  GREEN: add `"Agent": ("agents_system.agent.spec", "Agent")` to
  `__init__.py`'s `_EXPORTS` dict (`:66-80`); add the matching
  `"Agent": "agents_system.agent.spec.Agent"`-shaped entry to
  `tests/test_public_api.py`'s `_EXPECTED_EXPORTS` (`:26-40`) — both dicts
  stay hand-maintained and independently checked, per that test file's own
  existing pattern (confirmed by direct read: no self-referential check
  against `_EXPORTS` itself).
  Verify: `.venv/bin/pytest -q tests/test_public_api.py`
  Satisfies: "Agent locator discriminates exactly three source kinds" (public
  surface reachability — a downstream caller must be able to import `Agent`
  at all to use any of the three locator kinds this capability defines).
  <!-- sdd-owner: implementation -->

- [ ] **PR2-T5 — PR2 closing: docstrings + docs + full verification.**
  Add a module docstring to `agent/spec.py` (one paragraph, cross-referencing
  `design.md`'s D3/D6 and the spec's "Agent Python API" requirements — no new
  prose invented beyond what `design.md`/`spec.md` already state). Update
  `__init__.py`'s own module docstring code example (currently ending at "4.
  Turn it into a live, running agent...") with one short paragraph showing
  `Agent(name=..., extends=..., tools=..., permissions=...)` as an
  alternative to a bare platform-role string in step 3's `build_runtime`
  call, cross-referencing "see the library-first-agents ADR" without
  assuming the exact filename (PR7 has not landed yet in the same working
  tree — same forward-reference discipline `permission-model`'s PR3-T5 used).
  Verify: `.venv/bin/pytest -q` (full suite, run `tests/test_public_api.py`
  explicitly), `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/mypy src/`. Confirm `git diff --stat` against PR2's branch base;
  flag in the PR description if it exceeds ~400 lines rather than silently
  merging oversized.
  <!-- sdd-owner: implementation -->

---

## PR3 — Skills for importer agents

Branch: `feat/adr-004-3-agent-skills`
Scope: `src/agents_system/harness/loader.py` (`AgentDefinition` two new
fields); `src/agents_system/harness/factory.py` (`_load_skills` rewrite);
`docs/platform/deployment.md` + its `docs/platform_es/deployment.md` twin;
`docs/platform/role.md` + its `docs/platform_es/role.md` twin. Depends on
PR1a (`FolderLocator`/`InlineLocator` exist) and PR2
(`Agent(skill_contents=...)` is the Python-facing way to populate
`inline_skills`). Estimated lines: ~265 (design.md Testing Strategy table:
~230-260, revised up ~20 lines for the `docs/platform_es/` twin edits added
by PR3-T3, per the corrected `_es` doc-twin note above).

- [ ] **PR3-T1 — `AgentDefinition` gains `skills_folder`/`inline_skills` (D4).**
  RED: extend `tests/test_locator_folder.py`/`tests/test_locator_inline.py`
  (or a new `tests/test_agent_definition_skills_fields.py`) asserting: a
  platform-role `AgentDefinition` has `skills_folder=None,
  inline_skills={}` (both new fields default, unchanged output for every
  existing field-by-field `AgentDefinition` comparison in the existing
  suite — the explicit backward-compatibility regression this task exists to
  prove); a `FolderLocator`-sourced `AgentDefinition` has
  `skills_folder=<that folder's own path>`; an `InlineLocator`-sourced
  `AgentDefinition` (via `Agent(skill_contents={...})`) has
  `inline_skills={<name>: <content>}` populated from the `RawDefinition` PR2
  built.
  GREEN: add `skills_folder: pathlib.Path | None = None` and
  `inline_skills: Mapping[str, str] = dataclasses.field(default_factory=dict)`
  to `AgentDefinition` (`loader.py:192-220`). Populate `skills_folder` in
  `_load_role_files`'s `FolderLocator` branch (from `locator.path`) and leave
  it `None` for the platform/inline branches; populate `inline_skills` from
  `RawDefinition`'s (new, PR2-added) skill-content carrier when an
  `InlineLocator`'s `raw` supplies it — thread this through
  `_resolve_role_chain`'s fold and `resolve()`'s final `AgentDefinition`
  construction, both additive/defaulted so every existing call site that
  never sets either field is unaffected.
  Verify: `.venv/bin/pytest -q tests/platform_role_contract.py
  tests/test_harness_loader.py tests/test_agent_from_params.py`
  Satisfies: "Skills load from an importer agent's own folder" (the carrier
  half), "Skills load from inline Python-supplied content" (the carrier
  half). <!-- sdd-owner: implementation -->

- [ ] **PR3-T2 — `_load_skills` 4-source precedence (inline > own folder > deployment > error).**
  RED: create `tests/test_skills_precedence.py` covering spec Requirements
  "Skills load from an importer agent's own folder", "Skills load from inline
  Python-supplied content", "Inline skill content overrides a same-named
  folder skill", "Predefined-role deployment-only skills remain unchanged":
  an importer folder declaring `skills: [pricing-nuance]` with
  `skills/pricing-nuance.md` loads from that file with no `client`/deployment
  supplied, and the deployment-only `FactoryError` ("declares skills but no
  client deployment") is NOT raised for this agent; a missing importer-folder
  skill file raises `FactoryError` naming the missing skill and its expected
  path, in the same message shape the deployment-skills path already uses;
  `Agent(skill_contents={"tone": "Always answer in a formal register."})`'s
  `tone` skill content is exactly that string, with no file read;
  `Agent.from_folder(path, skills={"tone": "override text"})` overriding a
  folder's own `skills/tone.md` resolves to `"override text"`, not the
  file's content; a predefined role with no deployment and declared skills
  still fails exactly as it did before this change (unchanged regression);
  a predefined role's own `platform_root/roles/<name>/skills/` (if one
  happened to exist) is never treated as a skills source — only
  `deployments/{client}/{role}/skills/` is, for a predefined role.
  GREEN: rewrite `_load_skills` (`factory.py:124-166`) to the D4 four-step
  precedence per declared skill name: (1) `definition.inline_skills[name]` if
  present; (2) `definition.skills_folder / f"{name}.md"` if `skills_folder`
  is set and the file exists; (3) `deployments_root/{client}/{role_name}/
  skills/{name}.md` if `client is not None` (existing, unchanged mechanism);
  (4) none of the above → `FactoryError`, reworded to name all three sources
  it checked (not just "no client deployment," today's message assumption).
  Verify: `.venv/bin/pytest -q tests/test_skills_precedence.py
  tests/test_harness_factory.py`
  Satisfies: "Skills load from an importer agent's own folder", "An importer
  agent's own skills/ resolves with no deployment", "A missing
  importer-folder skill file fails the same way a missing deployment skill
  file does", "Skills load from inline Python-supplied content", "Inline
  skill content is used verbatim", "Inline skill content overrides a
  same-named folder skill", "Predefined-role deployment-only skills remain
  unchanged", "A predefined role with no deployment and declared skills still
  fails the same way", "A predefined role's own platform-role folder is never
  treated as a skills source". <!-- sdd-owner: implementation -->

- [ ] **PR3-T3 — Threat-matrix: importer skills/ symlink containment + doc contradiction fix.**
  RED: add `test_skill_symlink_does_not_escape_folder` to
  `tests/test_skills_precedence.py` (the threat-matrix RED test named in
  `design.md`'s Threat Matrix table): a `FolderLocator`-sourced importer
  folder's `skills/` directory contains a symlink pointing outside that
  folder, named to match a declared skill; assert `_load_skills` either
  reads only the file at the expected in-folder path (no escape — since
  `skills_folder` is always derived from an already-validated
  `FolderLocator.path`, never a caller-suppliable separate value, per D4's
  design response) or raises `FactoryError`, never silently reading content
  from outside the importer's own root.
  GREEN: no new containment code expected — `design.md`'s Threat Matrix row
  for this case states the property is inherited "for free" from
  `skills_folder` always being a validated `FolderLocator.path` derivative.
  If the RED test above fails for any reason other than "test file doesn't
  exist yet," treat it as a genuine gap in PR3-T2's implementation and add
  the minimal explicit resolved-path containment check
  (`Path.resolve()`/`is_relative_to`, same pattern PR1b's `_resolve_within_root`
  uses) rather than assuming the inheritance claim held. Separately, rewrite
  `docs/platform/deployment.md:91-93` (currently: "Skills are always
  client-specific. There are no generic skills. The `skills/` folder exists
  only in deployments.") to state the three sources and their precedence from
  D4; rewrite `docs/platform/role.md:7`'s "plus an optional `skills/`
  subdirectory" mention so it accurately describes the importer-folder skills
  source this PR adds (today's line already says "optional `skills/`
  subdirectory" for a role folder in general terms — confirm after this PR's
  code lands whether it needs a one-clause addition naming the importer-agent
  case explicitly, and add it if so). Apply the SAME two rewrites to their
  `docs/platform_es/` twins in this same task: `docs/platform_es/deployment.md:91-93`
  (confirmed present, Spanish text: "Las habilidades son siempre específicas
  del cliente. No existen habilidades genéricas de plataforma. La carpeta
  `skills/` existe únicamente dentro de los despliegues.") and
  `docs/platform_es/role.md:7`'s matching "subdirectorio opcional `skills/`"
  clause — translate the same three-sources-and-precedence content into
  Spanish, matching the existing `_es` files' informal `vos`-register tone
  (confirmed by direct read of both files during this tasks phase).
  Verify: `.venv/bin/pytest -q tests/test_skills_precedence.py`; no test
  command for the doc edits — confirm by direct read that
  `docs/platform/deployment.md`/`docs/platform/role.md` AND their
  `docs/platform_es/deployment.md`/`docs/platform_es/role.md` twins no
  longer contradict each other on skills location (the exact gap
  `proposal.md`'s Scope section names, in both languages).
  Satisfies: "Skills load from an importer agent's own folder" (the
  containment property), plus the proposal's own doc-contradiction Scope
  item. <!-- sdd-owner: implementation -->

- [ ] **PR3-T4 — PR3 closing: full verification.**
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR3's branch base; flag in the PR description if it
  exceeds ~400 lines. This PR is additive-only per `proposal.md`'s own
  Backward Compatibility & Migration item 8 — no `!`/`BREAKING CHANGE:`
  marker needed on its squash-merge commit.
  <!-- sdd-owner: implementation -->

---

## PR4a-i — `create_app` new params (unused by `lifespan()` yet)

Branch: `feat/adr-004-4a-i-create-app-params`
Scope: `src/agents_system/main.py` (`create_app`'s signature only — no
`lifespan()` body changes in this slice). Depends on PR2 (`Agent` type used
in the new params' type hints). Estimated lines: ~165. This is the
pre-split `design.md` itself flagged as a possible need ("PR4a split
recommendation") — split here because the combined PR4a's own design-time
estimate (~380-430) was already Medium-High risk, and the orchestrator
requested a pre-split rather than a measure-then-split during apply.

- [ ] **PR4a-i-T1 — `create_app` gains `agents`/`grants`/`clients` params + `_validate_runtime_id`.**
  RED: create `tests/test_create_app_agents_param.py` covering the signature
  half of spec Requirement "create_app accepts an explicit {id: Agent}
  registration": `create_app(registry_factory=..., agents={"acme-sales":
  "sales-agent"})` and `create_app(registry_factory=..., agents={"triage-bot":
  <an Agent instance>})` both construct a `FastAPI` app without error (the
  new params are accepted and stashed on `app.state`, but `lifespan()` does
  not yet read them in this slice — assert this explicitly: boot the app via
  the existing Settings-driven fallback path and confirm `agents`'s presence
  on `app.state` has no observable effect on `app.state.runtimes` yet, since
  PR4a-ii is what wires the consumption); `_validate_runtime_id("")` and
  `_validate_runtime_id("bad id with spaces")` both raise `DefinitionError`;
  `_validate_runtime_id("acme-sales-v2")` returns the id unchanged (reusing
  `loader._SAFE_SEGMENT`'s character class,
  `^[A-Za-z0-9][A-Za-z0-9_-]*$`, per D6).
  GREEN: add `agents: Mapping[str, "Agent | str"] | None = None`,
  `grants: Mapping[str, Sequence[str]] | None = None`, `clients: Mapping[str,
  str] | None = None` to `create_app`'s signature (`main.py:581-588`),
  stashed on `application.state.agents`/`.state.grants`/`.state.clients`
  alongside the existing `application.state.roots` assignment
  (`main.py:630-633`'s block). Add `_validate_runtime_id(id: str) -> str`
  (new, `main.py`) exactly per `design.md`'s D6, importing
  `loader._SAFE_SEGMENT`. No change to `lifespan()`'s body in this task —
  the new state attributes are written but not yet read.
  Verify: `.venv/bin/pytest -q tests/test_create_app_agents_param.py`
  Satisfies: "create_app accepts an explicit {id: Agent} registration" (the
  signature-acceptance half — end-to-end boot behavior is PR4a-ii),
  "Runtime ids are deployer-chosen, non-empty, and unique per registration"
  (the `_validate_runtime_id` half). <!-- sdd-owner: implementation -->

- [ ] **PR4a-i-T2 — PR4a-i closing: full verification.**
  Verify: `.venv/bin/pytest -q` (full suite — confirms the new unused params
  do not change any existing boot behavior), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR4a-i's branch base (PR3's branch, per the chain order);
  flag if it exceeds ~400 lines (unlikely at this size). No breaking-change
  marker needed yet — the params are additive and unconsumed until PR4a-ii.
  <!-- sdd-owner: implementation -->

---

## PR4a-ii — `lifespan()` loop rewritten around `agents`/`grants`/`clients`

Branch: `feat/adr-004-4a-ii-registration-wiring`
Scope: `src/agents_system/main.py` (`lifespan()`'s runtime loop,
`main.py:271-380`, including deleting the inline `model_id.split("__", 1)`
parser at `:280-281`). Depends on PR4a-i (params must exist before
`lifespan()` can consume them). Estimated lines: ~245. **This PR is
breaking** — see PR4a-ii-T4.

- [ ] **PR4a-ii-T1 — Predefined-role registration: byte-for-byte equivalent to today.**
  RED: extend `tests/test_main.py` with a registration-path test asserting
  `create_app(registry_factory=..., agents={"acme-sales": "sales-agent"},
  grants={"acme-sales": ["read:catalog"]}, clients={"acme-sales": "acme"})`
  boots and produces a runtime for `"acme-sales"` whose resolved
  `AgentDefinition`/tool surface is byte-for-byte identical to what the
  pre-this-PR `{deployment}__{role}` string
  (`"acme__sales-agent"`) resolution produced for the same role/client/grant
  (spec's "A predefined role is registered under a deployer-chosen id"
  scenario). Assert an empty-string id inside the `agents` mapping raises
  before any runtime is built (spec's "An empty-string id is rejected"
  scenario) and an arbitrary id with no `__`/no relationship to any role name
  (e.g. `"acme-support-v2"`) is accepted as-is with no parsing attempted
  (spec's "An arbitrary id string with no embedded convention is accepted"
  scenario).
  GREEN: rewrite `lifespan()`'s runtime loop (`main.py:271-380`) to iterate
  the **resolved** `{id: RoleLocator}` map built from `app.state.agents`
  (normalizing `Agent → Agent._to_locator()`, `str → str`, each id validated
  via PR4a-i's `_validate_runtime_id`), reading `app.state.grants`/
  `.state.clients` for the per-id grant/client lookups that today's loop
  derives by string-splitting `model_id`. Delete the inline
  `if "__" not in model_id: ... continue` / `prefix, role =
  model_id.split("__", 1)` block (`main.py:273-281`) entirely — this task is
  where that deletion actually happens (PR4b deletes the *env-parsing*
  counterpart, `openai_adapter.to_model_id`/`parse_model_id`, a distinct
  deletion). `resolve(role_or_locator, client=..., roots=roots)`, the
  WhatsApp/untrusted_input check (`:313-325`), the lease-timing check
  (`:331-342`), and the `DEPLOY_GRANTS`-lookup-before-`build_runtime`
  sequencing (`:344-374`) keep their exact substance — only their key moves
  from a parsed `(deployment, role)` pair to the caller's own opaque id plus
  the already-resolved locator, per `design.md`'s D5.
  Verify: `.venv/bin/pytest -q tests/test_main.py`
  Satisfies: "create_app accepts an explicit {id: Agent} registration", "A
  predefined role is registered under a deployer-chosen id", "Runtime ids are
  deployer-chosen, non-empty, and unique per registration", "An empty-string
  id is rejected", "An arbitrary id string with no embedded convention is
  accepted". <!-- sdd-owner: implementation -->

- [ ] **PR4a-ii-T2 — Custom `Agent` registration + `clients`-on-`Agent` misuse rejected.**
  RED: extend `tests/test_main.py` asserting `create_app(registry_factory=...,
  agents={"triage-bot": <Agent.from_folder(...) instance>},
  grants={"triage-bot": [...]})` boots and serves `"triage-bot"` through the
  same code paths (OpenAI adapter, WhatsApp binding eligibility) available to
  a predefined-role registration (spec's "An importer-defined custom agent is
  registered and served" scenario); `clients={"triage-bot": "acme"}` combined
  with an `Agent`-valued `agents["triage-bot"]` raises `DefinitionError` at
  boot (spec's own D5 rule: `clients` is only meaningful when `agents[id]` is
  a `str` — fail loud, not a silent no-op); a registration mapping with two
  valid entries and one entry whose `extends:` is unresolvable causes the
  **whole boot** to fail — the two valid entries are never served while the
  failing one is silently dropped (spec's "One bad registration entry blocks
  the whole boot" scenario).
  GREEN: in the rewritten loop from PR4a-ii-T1, add the `clients[id]` +
  `Agent`-valued-entry guard (`isinstance(agents[id], Agent) and id in
  clients: raise DefinitionError(...)`) before that id's `resolve()` call;
  confirm (no code change expected, but assert explicitly) that any
  unhandled exception raised while resolving one id propagates out of the
  loop rather than being caught-and-skipped — the loop's existing structure
  already has no per-id try/except, so this is a regression-guard assertion,
  not new production code, unless the RED test finds otherwise.
  Verify: `.venv/bin/pytest -q tests/test_main.py`
  Satisfies: "An importer-defined custom agent is registered and served",
  "No implicit client-override root is assumed for a registered agent" (the
  `clients`-on-`Agent` misuse half), "Boot fails loudly for every
  unresolvable registration, before serving begins", "One bad registration
  entry blocks the whole boot". <!-- sdd-owner: implementation -->

- [ ] **PR4a-ii-T3 — WhatsApp/adapter-binding-preserved regression suite.**
  RED: extend `tests/test_main.py` with the full set of WhatsApp/adapter
  regression scenarios the spec names for this capability, now expressed
  against the `agents`-driven registration path instead of the old
  `{deployment}__{role}` env strings: "A registered id is correctly bound to
  WhatsApp"; "An unmatched WhatsApp runtime id fails boot" (naming the
  unmatched id); "A trusted-input agent cannot be bound to WhatsApp"
  (`untrusted_input=False` on the WhatsApp-bound registration raises
  `DefinitionError` naming the agent and the runtime id); "An untrusted-input
  agent binds successfully"; "A WhatsApp-bound custom agent with too generous
  a timeout fails boot" (`ValueError` naming the runtime id and computed
  timeout values); "An adapter-named id appears in /v1/models" and "A
  WhatsApp-only runtime does not leak into /v1/models" (the adapter-exposed
  subset stays a separate, explicitly-named set from the full runtime cache,
  per spec's `/v1/models` requirement — confirm `app.state.adapter_model_ids`
  is still populated from an explicit adapter-exposed set, not from
  `agents`'s full key set); "A registered id without a DEPLOY_GRANTS entry
  fails boot" (renamed from the old `settings.deploy_grants` check to the new
  `grants` param, still `DefinitionError` naming the id); "A client-override
  registration with no RootConfig fails boot" (unchanged, `roots=None` case).
  GREEN: no new production logic expected beyond what PR4a-ii-T1/T2 already
  wired — these checks already exist substantively in the pre-this-PR loop
  (`main.py:303-359`) and were preserved, only re-keyed, by T1/T2. If any RED
  scenario above fails for a reason other than "not yet exercised via the new
  param shape," fix the narrowest gap in the T1/T2 loop rewrite. Confirm
  `app.state.adapter_model_ids` is now built from an explicit
  adapter-exposed parameter (not silently `frozenset(agents)`) — if
  `create_app` did not already have a way to name "which ids are
  adapter-exposed" separately from the full `agents` mapping before this
  task, add the minimal explicit parameter/derivation needed so spec's
  "/v1/models lists exactly the ids the operator named for the adapter"
  requirement holds, and note the addition in the PR description.
  Verify: `.venv/bin/pytest -q tests/test_main.py`
  Satisfies: "WhatsApp runtime binding resolves by registered id", "A
  registered id is correctly bound to WhatsApp", "An unmatched WhatsApp
  runtime id fails boot", "WhatsApp binding still requires
  untrusted_input=true", "WhatsApp binding still enforces the outbox lease
  timing budget", "/v1/models lists exactly the ids the operator named for
  the adapter", "An adapter-named id appears in /v1/models", "A WhatsApp-only
  runtime does not leak into /v1/models", "DEPLOY_GRANTS is keyed by the
  registered runtime id", "A registered id without a DEPLOY_GRANTS entry
  fails boot", "No implicit client-override root is assumed for a registered
  agent", "A client-override registration with no RootConfig fails boot".
  <!-- sdd-owner: implementation -->

- [ ] **PR4a-ii-T4 — PR4a-ii closing: full verification + BREAKING CHANGE marker.**
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR4a-ii's branch base; flag if it exceeds ~400 lines. **This
  PR's squash-merge subject MUST carry `!` and a `BREAKING CHANGE:` footer**
  (per `proposal.md`'s Backward Compatibility & Migration item 4 — "create_app's
  implicit string-resolution loop is gone" — this is the PR the orchestrator
  specifically named as breaking) so release-please's `bump-minor-pre-major:
  true` config records it at the next release, matching the mechanism the
  `permission-model` release already used (v0.1.0 → v0.2.0).
  <!-- sdd-owner: implementation -->

---

## PR4b — Runtime-id deletion + env migration

Branch: `feat/adr-004-4b-env-migration`
Scope: `src/agents_system/config.py` (`agent_registrations` field);
`src/agents_system/main.py` (`_parse_agent_registration` helper + the
Settings-driven fallback wiring when `agents is None`);
`src/agents_system/integration/openai_adapter.py` (`to_model_id`/
`parse_model_id` deletion); `tests/test_openai_adapter.py` (obsolete-case
deletion); `CHANGELOG.md` migration notes (release-please-authored at
release time, not directly edited by this PR — see PR6-T3's ground-truth
note on `CHANGELOG.md`). Depends on PR4a-ii (the `agents`-driven registration
path must exist before the env-boot path can build one from `Settings`).
Estimated lines: ~280 (design.md Testing Strategy table: ~260-300). **This
PR is breaking** — see PR4b-T4.

- [ ] **PR4b-T1 — `Settings.agent_registrations` + `_parse_agent_registration`.**
  RED: create `tests/test_agent_registrations_parsing.py` covering the env
  half of spec Requirements "The {deployment}__{role} scheme and its
  sentinel are removed" and "to_model_id/parse_model_id and the inline
  main.py parser are deleted": `Settings(agent_registrations=
  {"my-sales-bot": "sales-agent@acme", "support": "support-agent"})`
  populates the field via pydantic-settings' existing JSON-env decoding, an
  unset `AGENT_REGISTRATIONS` yields an empty dict (no exception at
  `Settings` construction time); `_parse_agent_registration("sales-agent@acme")`
  returns `("sales-agent", "acme")`; `_parse_agent_registration("support-agent")`
  returns `("support-agent", None)`; `_parse_agent_registration("bad@extra@value")`
  raises `DefinitionError` naming the malformed value (at most one `@`
  separator).
  GREEN: add `agent_registrations: dict[str, str] = {}` to `Settings`
  (`config.py`, same declarative pattern as `deploy_grants`'s existing field
  at `:169`), with a comment documenting the `"{role}"` /
  `"{role}@{client}"` value shape per `design.md`'s D5 (this field
  **replaces** the encoding `adapter_runtimes`/`whatsapp_runtime_id` used to
  carry — those two fields keep their existing `list[str]`/`str` types but
  their *values* become bare opaque ids, per Backward Compatibility item 1;
  update their docstrings at `config.py:94-99` and `:155-157` accordingly).
  Add `_parse_agent_registration(value: str) -> tuple[str, str | None]` (new,
  `main.py`, NOT `openai_adapter.py` — per D6, this is `main.py`'s own
  env-boot convention, not a public string-format contract).
  Verify: `.venv/bin/pytest -q tests/test_agent_registrations_parsing.py
  tests/test_config.py`
  Satisfies: "The {deployment}__{role} scheme and its sentinel are removed"
  (env-parsing half), "A legacy-shaped id string is treated as opaque, not
  parsed". <!-- sdd-owner: implementation -->

- [ ] **PR4b-T2 — Settings-driven fallback wiring when `agents is None`.**
  RED: extend `tests/test_main.py` with env-driven boot migration cases:
  `create_app(registry_factory=...)` called with no `agents` param (the
  default), but `AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme"}'`
  and matching `ADAPTER_RUNTIMES=["acme-sales"]`/`DEPLOY_GRANTS={"acme-sales":
  [...]}` set in the environment, boots and serves `"acme-sales"` identically
  to passing the equivalent `agents=`/`grants=`/`clients=` mapping directly
  (spec's Backward Compatibility framing: the env path and the explicit-param
  path converge on the same `lifespan()` loop); `demo.py:72-77`'s
  `build_app` call (`create_app(registry_factory=..., title="agents_system
  demo")`) needs **zero changes** and keeps working exactly as documented
  (assert this literally — call `build_app` unmodified and confirm it still
  boots with an empty runtime cache when no env vars are set, per D5's
  explicit callout).
  GREEN: in `lifespan()`, when `app.state.agents is None`, build the working
  `{id: locator}`/`{id: grant}`/`{id: client}` maps from
  `settings.agent_registrations` via `_parse_agent_registration` (id →
  role via `str` locator, id → client when the `@{client}` suffix was
  present), then feed them through the exact same loop body PR4a-ii-T1/T2
  built — no second, parallel loop implementation.
  Verify: `.venv/bin/pytest -q tests/test_main.py`
  Satisfies: "The {deployment}__{role} scheme and its sentinel are removed"
  (boot-path half), "A runtime id needs no _generic sentinel to mean 'no
  deployment'", "An id migrated from the old key format resolves correctly".
  <!-- sdd-owner: implementation -->

- [ ] **PR4b-T3 — Delete `to_model_id`/`parse_model_id`; delete obsolete tests.**
  RED: add `test_to_model_id_import_raises_import_error` (or extend
  `tests/test_openai_adapter.py`) asserting `from agents_system.integration
  .openai_adapter import to_model_id` raises `ImportError` after this task's
  GREEN change — the spec's own "Any external caller importing the deleted
  functions fails at import time" scenario, and add a repo-wide search
  assertion (a small `tests/test_no_duplicated_runtime_id_parsing.py`, or a
  `grep`-equivalent Python check) that no function anywhere in
  `src/agents_system/` splits a string on `"__"` to recover a
  deployment/role pair (spec's "No duplicated runtime-id parsing logic
  remains" scenario — this is the regression gate against a future
  reintroduction, not just proving today's deletion).
  GREEN: delete `to_model_id`/`parse_model_id`
  (`integration/openai_adapter.py:54-74`) entirely — no renamed/consolidated
  successor. Delete `tests/test_openai_adapter.py`'s test cases that
  exercised those two functions directly (confirm via direct read which
  cases in that file are `to_model_id`/`parse_model_id`-specific versus
  testing other adapter behavior that stays; delete only the former). Delete
  the now-dead `main.py:280-281` remnant comment/reference if PR4a-ii-T1
  left one (that task already removed the inline parsing block itself —
  this task is the confirming pass that nothing referencing the old
  `{deployment}__{role}` convention remains in `main.py`).
  Verify: `.venv/bin/pytest -q tests/test_openai_adapter.py
  tests/test_no_duplicated_runtime_id_parsing.py` (or the grep-equivalent
  check's own test file, per whichever name was used above)
  Satisfies: "to_model_id/parse_model_id and the inline main.py parser are
  deleted", "No duplicated runtime-id parsing logic remains", "Any external
  caller importing the deleted functions fails at import time".
  <!-- sdd-owner: implementation -->

- [ ] **PR4b-T4 — PR4b closing: full verification + BREAKING CHANGE marker.**
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR4b's branch base; flag if it exceeds ~400 lines. **This
  PR's squash-merge subject MUST carry `!` and a `BREAKING CHANGE:` footer**
  (per `proposal.md`'s Backward Compatibility & Migration items 1-3 — the
  runtime-id scheme removal, the `DEPLOY_GRANTS` key rewrite, and the
  `to_model_id`/`parse_model_id` deletion all land in this PR; this is the
  other PR the orchestrator specifically named as breaking — "the PR that
  removes the runtime-id scheme"), so release-please records it.
  <!-- sdd-owner: implementation -->

---

## PR5 — Terminology rename (generic → predefined) + `examples/` relocation

Branch: `feat/adr-004-5-predefined-rename-examples`
Scope: identifiers/comments across `src/agents_system/` and `tests/` wherever
"generic" currently means "one of the eight packaged roles" (not the abstract
`base -> agent` tree, which keeps the word "generic" per spec's own
Terminology section); `docs/platform/*.md` and `docs/architecture/*.md`
prose using the old term the same way, PLUS their `docs/platform_es/*.md`/
`docs/architecture_es/*.md` twins wherever the same rename applies;
`src/agents_system/demo.py` → `examples/demo/app.py`;
`docs/platform/demo-entrypoint.md` and its `docs/platform_es/demo-entrypoint.md`
twin. Depends on PR4b (the rename touches `main.py`/`config.py` comments
this PR's predecessor already rewrote substantively — rename lands last so
it never conflicts with PR4a-ii/PR4b's own in-flight edits to those files).
Estimated lines: ~240 (design.md Testing Strategy table: ~150-200, revised
up for the `docs/platform_es/demo-entrypoint.md` twin's multi-place env-scheme
update in PR5-T2 and the `docs/platform_es/role.md`/`docs/platform_es/deployment.md`
rename pass in PR5-T1, per the corrected `_es` doc-twin note above). **This
PR is breaking** — see PR5-T3.

- [ ] **PR5-T1 — Rename "generic role(s)" → "predefined role(s)" (identifiers, comments, error messages, docs).**
  RED: extend `tests/test_untrusted_input_invariant.py`/
  `tests/test_harness_loader.py` (whichever already assert on a role-load
  failure message's exact text) with cases asserting a predefined-role load
  failure's error message uses "predefined role", never "generic role" (spec's
  "A predefined-role load failure uses the new term" scenario); assert a
  message referring to `platform/roles/agent` itself may still say "generic
  agent" (spec's "A reference to the base->agent tree is unaffected"
  scenario — this is a MUST-NOT-rename case, add it as an explicit negative
  assertion so a future overzealous rename cannot regress it). Add a
  `discover_platform_roles`-adjacent assertion (in
  `tests/platform_role_contract.py`) that discovery's own docstring/comment
  language, if it says "generic," now says "predefined" where it means the
  eight packaged roles.
  GREEN: rename identifiers/comments/error-message text wherever "generic"
  currently distinguishes the eight packaged roles from everything else —
  scoped exactly per spec's Terminology section: `harness/loader.py`'s
  error-message strings naming a role-not-found/malformed-role case;
  `tests/platform_role_contract.py`'s own comments (`EXPECTED_ROLE_TOOLS`'s
  docstring, `discover_platform_roles`'s docstring); `docs/platform/role.md`,
  `docs/platform/deployment.md` prose using "generic role"/"generic roles" to
  mean the eight; `__init__.py`'s module docstring's "GENERIC harness and the
  GENERIC platform agent roles" line (the harness stays "generic"; the roles
  become "predefined"). Do NOT rename any reference to `platform/roles/agent`
  itself, the abstract tree every predefined role extends (spec explicitly
  keeps "generic agent" for that usage) — grep for every "generic" occurrence
  across the scoped files before editing, and classify each one against
  spec's own two Terminology-section examples before deciding to rename it.
  Apply the SAME classify-then-rename pass to `docs/platform_es/role.md` and
  `docs/platform_es/deployment.md` (both confirmed present during this tasks
  phase, using "rol genérico"/"roles genéricos" the same distinguishing way
  the EN files do — e.g. `docs/platform_es/deployment.md:87`'s "el rol
  genérico" ceiling-language, `docs/platform_es/role.md`'s own role-folder
  prose): rename "rol genérico"/"roles genéricos" → "rol predefinido"/"roles
  predefinidos" wherever it means one of the eight packaged roles; leave
  "agente genérico" (the abstract `base -> agent` tree) unrenamed, matching
  the EN spec's own generic-agent exception. Do not translate or rename any
  code identifier inside the `_es` files (they are prose-only docs).
  Verify: `.venv/bin/pytest -q` (full suite — a message-text rename risks
  breaking an existing test asserting the OLD wording; run everything, not
  just the new assertions); no test command for the `_es` doc edits — confirm
  by direct read that `docs/platform_es/role.md`/`docs/platform_es/deployment.md`
  use "predefinido" consistently with their EN twins' new terminology.
  Satisfies: "Error and boot-failure messages name predefined roles by their
  new term", "A predefined-role load failure uses the new term", "A
  reference to the base->agent tree is unaffected".
  <!-- sdd-owner: implementation -->

- [ ] **PR5-T2 — Move `demo.py` → `examples/demo/app.py`; update `demo-entrypoint.md`.**
  RED: add `tests/test_demo_entrypoint.py`-scoped assertions (extending that
  existing file, confirmed present) that `agents_system.demo` no longer
  exists as an importable module (`import agents_system.demo` raises
  `ModuleNotFoundError`/`ImportError` — spec's "The installed package
  contains no application entrypoint" scenario) and that
  `examples/demo/app.py` exists and exposes the same `build_app`/`main`
  surface the old module did (spec's "An equivalent example entrypoint
  exists outside the package" scenario).
  GREEN: move `src/agents_system/demo.py` → `examples/demo/app.py` verbatim
  apart from its module docstring (remove the now-inapplicable `python -m
  agents_system.demo` invocation language, replace with `python
  examples/demo/app.py`) and the `if __name__ == "__main__":` guard's
  module-path comment. No `__init__.py` in `examples/` or `examples/demo/` —
  confirmed by `design.md`'s D7 that `pyproject.toml`'s package discovery
  already excludes `examples/` from any packaged artifact. Update
  `docs/platform/demo-entrypoint.md`'s "## 4. Run it" section from `uv run
  python -m agents_system.demo` to `python examples/demo/app.py`, and every
  `ADAPTER_RUNTIMES`/`DEPLOY_GRANTS` example value in that same file from
  the old `"_generic__sales-agent"`/`{"_generic__sales-agent": [...]}` shape
  to a chosen opaque id shape consistent with PR4b's `AGENT_REGISTRATIONS`
  scheme (e.g. `ADAPTER_RUNTIMES='["demo-sales-agent"]'`,
  `AGENT_REGISTRATIONS='{"demo-sales-agent": "sales-agent"}'`,
  `DEPLOY_GRANTS='{"demo-sales-agent": [...]}'`) — this file currently cites
  the old scheme in at least four places (the variables table, the worked
  `export` block, the curl `-d` body's `"model"` value, and the "## 2.
  Configure the environment" prose); update every one of them, not just the
  invocation line. Apply the identical set of updates, translated to
  Spanish and matching its existing `vos`-register tone, to
  `docs/platform_es/demo-entrypoint.md` (confirmed present during this tasks
  phase, with the SAME four old-scheme citations at its own "## 2. Configurá
  el entorno" table, its `export` block, its curl `-d` body's `"model"`
  value `"_generic__sales-agent"`, and its "## 4. Corrélo" invocation
  `uv run python -m agents_system.demo`) — this is one task updating two
  files, not two separate doc tasks, so the EN and ES twins never drift
  relative to each other mid-PR.
  Verify: `.venv/bin/pytest -q tests/test_demo_entrypoint.py`; manual
  verification only for the doc's own runnability (no CI command runs
  `docs/platform/demo-entrypoint.md`/`docs/platform_es/demo-entrypoint.md`
  end-to-end) — confirm by direct read that every example in BOTH files is
  internally consistent with PR4b's env scheme, in their respective
  languages.
  Satisfies: "No default deployments path ships inside the installable
  package", "The installed package contains no application entrypoint", "An
  equivalent example entrypoint exists outside the package".
  <!-- sdd-owner: implementation -->

- [ ] **PR5-T3 — PR5 closing: full verification + BREAKING CHANGE marker.**
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR5's branch base; flag if it exceeds ~400 lines. **This
  PR's squash-merge subject MUST carry `!` and a `BREAKING CHANGE:` footer**
  (per `proposal.md`'s Backward Compatibility & Migration item 6 —
  `agents_system.demo` module path removed, `python -m agents_system.demo`
  stops working) so release-please records it. The terminology rename itself
  (item 5) is explicitly flagged "low external-impact" by the proposal and
  does not independently require the marker — the `demo.py` removal is what
  makes this PR breaking.
  <!-- sdd-owner: implementation -->

---

## PR6 — Guard 1 governance

Branch: `feat/adr-004-6-guard1-governance`
Scope: `tests/platform_role_contract.py` (`RoleGovernanceSnapshot`,
`EXPECTED_ROLE_SURFACE`, new contract test); new
`scripts/check_role_governance_marker.py` +
`tests/test_check_role_governance_marker.py`;
`.github/workflows/ci.yml` (new job). Depends on PR1a (discovery/
`FolderLocator` types must exist so Guard 1's own "importer agents are out
of scope" requirement has something concrete to test against) and PR5 (the
rename lands first so this PR's new code uses "predefined role" throughout,
never "generic role"). Estimated lines: ~320 (design.md Testing Strategy
table: ~300-340).

- [ ] **PR6-T1 — `RoleGovernanceSnapshot` + `EXPECTED_ROLE_SURFACE` + contract test.**
  RED: extend `tests/platform_role_contract.py`'s own test suite (or a new
  `tests/test_role_governance_contract.py`, implementer's choice — keep the
  snapshot/dataclass definitions themselves in `platform_role_contract.py`
  per D8, matching `EXPECTED_ROLE_TOOLS`'s existing home) with: an
  unmodified predefined role (fixture-scoped `RoleGovernanceSnapshot` +
  fixture-scoped "currently resolved surface," NOT a real platform role edit
  — this task must not touch any real `platform/roles/*/manifest.md`) finds
  no divergence when compared against its own snapshot (spec's "An
  unmodified predefined role matches its snapshot" scenario); a fixture
  divergence (tools/permissions differ from the fixture snapshot) with
  **neither** a version bump nor a changelog-entry stand-in fails, naming the
  role, the exact diverged tools/permissions, and both missing conditions
  (spec's "Failure output is actionable" scenario); a fixture divergence with
  a version bump but no changelog-entry stand-in still fails, naming only the
  missing changelog condition (spec's "A tools change with a version bump but
  no CHANGELOG entry still fails" scenario); a fixture divergence with a
  changelog-entry stand-in but no version bump still fails, naming only the
  missing version-bump condition (spec's "A tools change with a CHANGELOG
  entry but no version bump still fails" scenario); a fixture divergence with
  BOTH conditions satisfied passes (spec's "A tools change with both a
  version bump and a CHANGELOG entry passes" scenario); a `role.md`-only
  prose edit (no `tools`/`permissions` change) triggers no version-bump/
  CHANGELOG requirement at all (spec's "A non-tools, non-permissions manifest
  edit does not trigger the guard" scenario); the check's role-discovery
  scope is exactly `platform_role_contract.discover_platform_roles()`'s own
  set — an importer-defined agent (a `FolderLocator`/`Agent` fixture from
  PR1a/PR2's own test fixtures) extending a predefined role and adding its
  own tool is never subject to this guard (spec's "An importer agent
  extending a predefined role changing its own added tools triggers no Guard
  1 obligation" scenario); the snapshot never re-derives itself from the
  manifest it is checking — editing only the manifest fixture (not the
  snapshot fixture) still reports a divergence (spec's "The snapshot does not
  grade a role against itself" scenario).
  GREEN: add `RoleGovernanceSnapshot` (frozen dataclass: `tools:
  frozenset[str]`, `permissions: frozenset[str]`, `version: str`) and
  `EXPECTED_ROLE_SURFACE: dict[str, RoleGovernanceSnapshot]` (seeded from the
  CURRENT resolved surface of each of the eight predefined roles at this
  PR's merge time — a reviewed, deliberate copy, same philosophy
  `EXPECTED_ROLE_TOOLS` already documents) to `tests/platform_role_contract.py`,
  exactly per `design.md`'s D8 code block. Add the new pytest contract test:
  for each role in `PINNED_ROLES` (reusing the existing discovery set — spec's
  own "Discovery used by Guard 1 matches discovery used by boot guards"
  requirement, satisfied by reuse, not a parallel discovery), resolve it,
  compare `(tools, permissions)` against `EXPECTED_ROLE_SURFACE[role]`; on a
  diff, require the resolved role's `version`'s MAJOR component to be
  strictly greater than the snapshot's AND a `CHANGELOG.md` entry
  "identif[ying] the affected role and change" (spec's own condition-(b)
  wording — implement this as a simple substring/role-name-presence check
  against `CHANGELOG.md`'s content, not a full parser, per D8's own framing
  of this as an enforceable-at-PR-time check); fail naming exactly what
  diverged and which condition(s) are unmet when only one or neither holds.
  Verify: `.venv/bin/pytest -q tests/platform_role_contract.py` (or the new
  file's own path, per whichever name was used above)
  Satisfies: "A versioned, independent snapshot of every predefined role's
  surface exists", "The snapshot does not grade a role against itself", "An
  unmodified predefined role matches its snapshot", "A contract check fails
  on any tools/permissions divergence lacking both a version bump and a
  CHANGELOG entry" (all four scenarios), "The contract check names the exact
  divergence", "Failure output is actionable", "Guard 1 applies only to
  roles discovered under platform_root/roles" (both scenarios).
  <!-- sdd-owner: implementation -->

- [ ] **PR6-T2 — CI marker-detection script + `.github/workflows/ci.yml` job.**
  RED: create `tests/test_check_role_governance_marker.py` (a pure unit test
  over the marker-detection function in isolation — D8's own suggested
  approach, chosen over a fixture-PR-diff integration test since this repo
  has no existing workflow that exercises a synthetic PR): a list of commit
  messages where at least one subject line contains `!` before its `:` (e.g.
  `"feat(roles)!: widen sales-agent tools"`) returns `True`; a list where at
  least one message body contains a `BREAKING CHANGE:` footer returns `True`;
  a list with neither anywhere returns `False`; an empty list returns
  `False`.
  GREEN: create `scripts/check_role_governance_marker.py` with
  `commit_messages_carry_breaking_marker(messages: list[str]) -> bool`
  (checks each message's first line for a `!` immediately before the first
  `:`, per conventional-commits' own breaking-change shorthand, OR the
  literal substring `"BREAKING CHANGE:"` anywhere in the message) and a
  `main()` that: reads the changed-files list for the current PR/push (via
  `git diff --name-only <base>...<head>`, matching the pattern
  `gitleaks-action`'s own job comment describes for `pull_request` vs.
  `push` — reuse that same base/head resolution convention rather than
  inventing a new one); if no `platform/roles/**/manifest.md` path is among
  them, exits 0 immediately (the guard does not apply); otherwise reads `git
  log <base>..<head> --format=%B` for the commit range and exits 0 only if
  `commit_messages_carry_breaking_marker` returns `True` for at least one
  commit in range, else exits 1 with a message naming the changed manifest
  path(s) and stating a `!`/`BREAKING CHANGE:` marker is required. Add a new
  job to `.github/workflows/ci.yml` (the exact file confirmed present at
  `.github/workflows/ci.yml`, per this tasks phase's own `Glob` check — no
  other existing workflow runs on `platform/roles/**` changes, confirmed by
  direct read of `ci.yml` and `release-please.yml`): name it
  `predefined-role-governance-marker`, `runs-on: ubuntu-latest`, checkout
  with `fetch-depth: 0` (same requirement `secret-scan`'s job comment
  documents, for the same reason — the commit range must be locally walkable),
  install dependencies via the same `uv sync --group dev` step every other
  job uses, and run `.venv/bin/python scripts/check_role_governance_marker.py`.
  Verify: `.venv/bin/pytest -q tests/test_check_role_governance_marker.py`;
  no CI-integration test command applies for the workflow YAML itself —
  confirm by direct read that the new job's steps match every sibling job's
  checkout/setup-uv/install pattern in `ci.yml`.
  Satisfies: "A contract check fails on any tools/permissions divergence
  lacking both a version bump and a CHANGELOG entry" (the CI-enforced half —
  D8's own two-halves split: the pytest contract test from PR6-T1 checks the
  snapshot/version-bump condition at PR time, this task's CI job checks the
  commit-marker condition that actually produces the eventual CHANGELOG
  entry). <!-- sdd-owner: implementation -->

- [ ] **PR6-T3 — PR6 closing: `CHANGELOG.md` ground-truth confirmation + docs + full verification.**
  Confirm by direct read that `CHANGELOG.md` already exists at the
  repository root (the `[0.2.0]` section) and that
  `release-please-config.json`'s `changelog-path: "CHANGELOG.md"`
  (confirmed at `release-please-config.json:22`) already points at it — this
  satisfies spec's "CHANGELOG.md exists and is the recorded location for
  Guard 1 entries" requirement as-is; do NOT create a new `CHANGELOG.md` or
  overwrite the existing one (unlike `permission-model`'s PR1-T6/PR4-T4,
  which correctly found no file existed at that point in the repo's history
  — this is the opposite ground-truth state, and the governance spec's own
  "none exists prior to this change" phrasing is stale per `design.md`'s D8
  ground-truth correction, carried forward verbatim at the top of this
  file). Add a module docstring note to the new `RoleGovernanceSnapshot`/
  `EXPECTED_ROLE_SURFACE` block (already drafted in PR6-T1) cross-referencing
  `design.md`'s D8 and spec's Guard 1 requirements.
  Verify: `.venv/bin/pytest -q` (full suite), `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy src/`. Confirm `git diff
  --stat` against PR6's branch base; flag if it exceeds ~400 lines. No
  breaking-change marker — this PR is CI/governance-only, no runtime
  behavior change.
  <!-- sdd-owner: implementation -->

---

## PR7 — ADR-004 + ADR-002 amendments

Branch: `docs/adr-004-7-adr-and-pointers`
Scope: `docs/` only — no source or test changes. New
`docs/architecture/adr-004-library-first-agents.md` AND its
`docs/architecture_es/adr-004-library-first-agents.md` twin; pointer
amendments to `docs/architecture/adr-002-agent-model-and-capabilities.md`
AND `docs/architecture_es/adr-002-agent-model-and-capabilities.md`. Depends
on PR1a-PR6 having landed (ADR-004 documents shipped, not proposed,
behavior — same discipline `permission-model`'s PR4 followed for ADR-003,
which also shipped both an EN and an `_es` twin — confirmed by direct read:
`docs/architecture_es/adr-003-permission-hierarchy.md` exists alongside its
EN original). Estimated lines: ~290 (design.md Testing Strategy table:
~150-200 for the EN-only content, revised up ~90-140 lines for the new
`docs/architecture_es/adr-004-library-first-agents.md` twin plus the
`docs/architecture_es/adr-002-agent-model-and-capabilities.md` pointer
notes, per the corrected `_es` doc-twin note above — `adr-003-permission-hierarchy.md`'s
EN/ES twins are each ~75 lines, used here as the closest size reference for
a new ADR of comparable scope).

- [ ] **PR7-T1 — Write `docs/architecture/adr-004-library-first-agents.md` + its `_es` twin.**
  New ADR, following this repo's existing ADR structure (see
  `adr-001-runtime-topology.md`/`adr-002-agent-model-and-capabilities.md`/
  `adr-003-permission-hierarchy.md` for section conventions: Summary,
  Context, Decision, Corrections if any). Content: the `RoleLocator`
  discriminated union (D1) and the three source kinds it unifies; the
  `extends:` fail-loud algorithm, its 8 enumerated cases, and the D2 Resolved
  Decision on `..` containment being checked after resolution rather than
  rejected on sight (D2); the `Agent` Python API's shape and its
  folder/params compose precedence (D3); the skills 4-source precedence
  (D4); the registration redesign — deployer-chosen ids, `{id: Agent}`
  mapping, the deleted `{deployment}__{role}`/`_generic` scheme,
  `to_model_id`/`parse_model_id`'s deletion (D5, D6); the
  `examples/demo/app.py` relocation and why no `python -m` entry remains
  (D7); Guard 1's versioned snapshot + two-halves enforcement mechanism (D8).
  State explicitly which ADR-002 sections it amends: A.1 (`:94` — the
  package's own stated scope, now including importer-defined agents), D
  (`:1208`), C.15 (`:1131`), E.18 (`:1360`), F (`:1429`) — per `proposal.md`'s
  own Affected Areas citations, re-confirmed by direct read during PR7 (line
  numbers may have shifted since the proposal was written if PR1a-PR6 touched
  `adr-002-agent-model-and-capabilities.md` at all — they should not have,
  per this file's own Cross-PR Ground Rules, so re-confirm rather than
  assume). Write `docs/architecture_es/adr-004-library-first-agents.md` as a
  full Spanish twin in the SAME task (matching `docs/architecture_es/adr-003-permission-hierarchy.md`'s
  existing tone/register and section structure, confirmed by direct read —
  a dense, formal-register translation, distinct from `docs/platform_es/`'s
  more informal `vos`-register prose docs) — translate every D1-D8 section,
  not a summary; its ADR-002-section citations (A.1/D/C.15/E.18/F) point at
  the `docs/architecture_es/adr-002-agent-model-and-capabilities.md` line
  numbers this tasks phase confirmed by direct read: A.1 `:101`, D `:1351`,
  C.15 `:1266`, E.18 `:1516`, F `:1594` (re-confirm at task time, same
  caveat as the EN numbers above).
  Verify: no test command applies (docs-only); confirm both
  `docs/architecture/adr-004-library-first-agents.md` and
  `docs/architecture_es/adr-004-library-first-agents.md` exist, every D1-D8
  design decision is present as a heading or named section in BOTH, and the
  ES file's ADR-002 section citations match the ES line numbers above (not
  the EN ones).
  <!-- sdd-owner: implementation -->

- [ ] **PR7-T2 — Amend ADR-002's superseded/amended sections (EN + `_es`).**
  Add a short "See ADR-004" pointer note at A.1, D, C.15, E.18, and F in
  `docs/architecture/adr-002-agent-model-and-capabilities.md` (re-confirm
  each section's current line number by direct read at task time, per
  PR7-T1's own caveat above), one or two sentences each, linking to
  `adr-004-library-first-agents.md`, without deleting or rewriting the
  original section text (the original text remains the historical record of
  what those sections originally decided for the eight-predefined-role-only
  world; ADR-004 is what actually ships on top of it now). Do not alter any
  other part of `adr-002-agent-model-and-capabilities.md`. Add the SAME five
  pointer notes, in Spanish, to `docs/architecture_es/adr-002-agent-model-and-capabilities.md`
  at its own confirmed section anchors — A.1 `:101`, D `:1351`, C.15 `:1266`,
  E.18 `:1516`, F `:1594` (re-confirm each by direct read at task time, same
  discipline as the EN pass) — linking to
  `adr-004-library-first-agents.md` (the EN filename — `docs/architecture_es/`
  files link to their EN-named counterpart, matching the existing
  cross-reference convention in `docs/architecture_es/adr-003-permission-hierarchy.md`,
  confirm this convention by direct read before writing the links). Do not
  alter any other part of either file.
  Verify: no test command applies (docs-only); confirm exactly five pointer
  notes were added to EACH of the two files (ten total) and no other line in
  either file changed (`git diff docs/architecture/adr-002-agent-model-and-capabilities.md
  docs/architecture_es/adr-002-agent-model-and-capabilities.md` should show
  only small additive hunks at the five cited anchors per file).
  <!-- sdd-owner: implementation -->

- [ ] **PR7-T3 — PR7 closing: `state.yaml` final pass + full consistency check.**
  Update `openspec/changes/library-first-agents/state.yaml`: `phase: archive`
  is NOT set by this task (archive happens separately, at merge, per
  `AGENTS.md`'s SDD-flow mapping — `Closes #NN`-style issue linkage is a
  PR-level concern, not this file's). Only confirm `artifacts.tasks: true`
  and `tasks_progress` are current. Read through every PR1a-PR7 task above
  once more and confirm every spec requirement title in `specs/
  agent-definition-locator/spec.md`, `specs/agent-registration-serving/
  spec.md`, and `specs/predefined-agent-governance/spec.md` is referenced by
  at least one task's "Satisfies:" line; note any gap found as a new
  follow-up task rather than silently leaving it uncovered.
  Verify: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`
  (guard against any accidental source edit having crept into a docs-only
  branch); confirm `git diff --stat` for PR7 touches only files under
  `docs/` and `openspec/changes/library-first-agents/state.yaml`, at or below
  ~300 lines (revised up from the original ~200 for the `docs/architecture_es/`
  twin work added in PR7-T1/PR7-T2).
  <!-- sdd-owner: implementation -->

---

## Follow-ups (not part of this change)

- `Agent` exposing `command_tools` (ADR-002 C.12 declarative command tools)
  is explicitly out of scope for this change (`design.md`'s Open Questions):
  an importer `Agent` cannot declare a command tool after this change ships.
  File a GitHub issue for it (per the orchestrator's own example, `#65`) —
  not created by this tasks phase; left for the user/orchestrator to file,
  labeled independently of this change's ten PR slices.
- Per-principal identity (issue `#15`, ADR-002 A.2) stays role/deploy-scoped
  — `proposal.md`'s own Out of Scope section; no task above touches it.
- ADR-005's operational-safety roadmap items are referenced as context only,
  not re-planned by any task in this file.
- `openspec/specs/permission-hierarchy/spec.md`'s promotion from the archived
  `permission-model` change folder to `openspec/specs/` (a process-only gap
  `proposal.md`'s own Modified Capabilities section flagged and deferred) is
  not fixed by any task above — it remains a separate housekeeping item.
