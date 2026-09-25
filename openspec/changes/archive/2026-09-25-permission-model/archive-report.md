# Archive Report: permission-model

**Change**: permission-model
**Archived to**: `openspec/changes/archive/2026-09-25-permission-model/` (openspec/hybrid)
**Archived by**: SDD verify + archive pass, 2026-09-25, run against `origin/main` @ `d3fad1f` in worktree `perm-archive` (branch `docs/permission-model-archive`).

## Final-State Authority

This report is the terminal record of the `permission-model` cycle. Ranking sources per the archive contract:

1. **Persisted tasks artifact** (`tasks.md`, carried into this archive folder): all 21 tasks across PR1-PR4 are checked `[x]`; `pending: []` in `state.yaml`.
2. **This session's launch context**: the four PRs (#43, #50, #55, #57) are confirmed merged into `origin/main`, and issue #38 is confirmed `CLOSED` on GitHub — corroborating `tasks.md`'s recorded completion rather than contradicting it.
3. **`verify-report.md`** (this same archive folder, written immediately before this report, 2026-09-25): intermediate snapshot confirming 21/21 spec requirements PASS against shipped code, full test suite green (1306 passed, 93 deselected, 18 xfailed), `ruff check` clean, `mypy src/` clean.

No contradiction was found between these sources — all three agree the change is fully implemented, tested, and documented on `origin/main`.

## Specs Synced

| Domain | Action | Details |
|---|---|---|
| `permission-hierarchy` | Created | New capability — no existing `openspec/specs/permission-hierarchy/spec.md` prior to this archive (confirmed: the directory did not exist in the worktree before this run). The full delta spec (30k, 563 lines, all 21 requirements) was mechanically copied verbatim — `diff` readback against the source was empty (exit 0) both immediately after the copy and again as part of the whole-folder `diff -r` archive readback. No text edit was needed: the on-disk delta spec already named the error root `AgentPermissionError`, matching shipped code exactly (see verify-report.md, Documented Deviation 1) — the only discrepancy found was in a *stale Engram-cached* observation, not the file synced here. |

## Archive Contents

- `proposal.md`: present
- `specs/permission-hierarchy/spec.md`: present (the same file now also at `openspec/specs/permission-hierarchy/spec.md`, the new source of truth)
- `design.md`: present (includes Resolved Decisions 1-5, owner-approved 2026-09-24)
- `exploration.md`: present
- `tasks.md`: present — 21/21 tasks complete (`PR1-T1`...`PR1-T6`, `PR2-T1`...`PR2-T6`, `PR3-T1`...`PR3-T5`, `PR4-T1`...`PR4-T4`); 0 pending
- `verify-report.md`: present — 21/21 requirements PASS, 0 PARTIAL, 0 FAIL
- `state.yaml`: present, updated to `phase: archived` before the move

## Source of Truth Updated

- `openspec/specs/permission-hierarchy/spec.md` now reflects the shipped `Permission`/`Tier` class hierarchy, `PermissionRegistry`, R1-R4 enforcement, and the persisted deploy grant ceiling (issue #38 fix).

## Mechanical Copy / Move Evidence

- Delta-spec → main-spec copy: `cp` to a temp file, `diff` (source vs. temp) → **empty, exit 0**, then `mv` into place.
- Change-folder → archive move: pre-move recursive snapshot (`cp -R`) to a scratch directory, `git mv openspec/changes/permission-model openspec/changes/archive/2026-09-25-permission-model` (exit 0), confirmed source path absent, `diff -r` (snapshot vs. archived destination) → **empty, exit 0**. `archive-report.md` (this file) was written after that readback, consistent with the contract's additive-file exclusion.

## Verification Summary (from verify-report.md, same folder)

- Full suite: `pytest -q` → **1306 passed, 93 deselected, 18 xfailed**, 7 pre-existing/unrelated warnings.
- `ruff check .` → all checks passed.
- `mypy src/` → no issues in 69 source files.
- 21/21 spec requirements traced to implementing code and a covering test, verdict PASS.
- 6 documented deviations recorded (error root class name — already reflected in the synced spec text; grant-ceiling declared-permission bound; class/name grant equivalence; explicit `tier_rank`; atomic `get_or_register`; deferred imports) — all safety-preserving refinements consistent with spec intent, none a FAIL.
- 1 new open follow-up identified: a library-only code path (`build_runtime` called with a class-form grant that was never registered in `PermissionRegistry`) can defer an `UnknownPermissionNameError` from grant/boot time to first-turn execution in `AgentRuntime.run_turn`'s default. Not part of this change's scope or any of its 21 tasks; recommended for a follow-up GitHub issue, the same pattern issue #47 used for the (now-closed, via PR #58) evals implicit-grant follow-up.

## SDD Cycle Complete

The change is archived. Implementation: **complete** — all four PRs (#43, #50, #55, #57) merged to `origin/main`; issue #38 closed. Verification: **complete**, 21/21 PASS, full suite/lint/type-check green.
Unfinished tasks: none. Unresolved findings: none blocking; one new open follow-up noted above for separate tracking (not filed by this archive pass — filing a new issue is outside this SDD change's edit scope).
