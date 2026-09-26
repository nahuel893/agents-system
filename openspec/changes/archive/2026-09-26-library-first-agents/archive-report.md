# Archive Report: library-first-agents

**Change**: library-first-agents (ADR-004)
**Archived to**: `openspec/changes/archive/2026-09-26-library-first-agents/` (openspec/hybrid)
**Archived by**: SDD verify + archive pass, 2026-09-26, run against `origin/main` @ `04eeb78` in worktree `lfa-archive` (branch `docs/library-first-agents-archive`).

## Final-State Authority

This report is the terminal record of the `library-first-agents` cycle. Ranking sources per the archive contract:

1. **Persisted tasks artifact** (`tasks.md`, carried into this archive folder): all tasks across PR1a-PR7 are checked `[x]`; `pending: []` in `state.yaml`.
2. **This session's launch context**: all ten PR slices (#68, #70, #85, #91, #92, #95, #98, #101, #103, #111) are confirmed merged into `origin/main` (`git log origin/main`).
3. **`verify-report.md`** (this same archive folder, written immediately before this report, 2026-09-26): intermediate snapshot confirming 36/37 spec requirements PASS against shipped code, one documented non-blocking test-coverage gap, full test suite green (2290 passed, 184 deselected, 17 xfailed), `ruff check` clean, `ruff format --check` clean, `mypy src/` clean.

No contradiction was found between these sources — all three agree the change is fully implemented, tested, and documented on `origin/main`.

## Specs Synced

| Domain | Action | Details |
|---|---|---|
| `agent-definition-locator` | Created | New capability — no existing `openspec/specs/agent-definition-locator/spec.md` prior to this archive. The full delta spec was mechanically copied verbatim — `diff` readback against the source was empty (exit 0). |
| `agent-registration-serving` | Created | New capability — no existing `openspec/specs/agent-registration-serving/spec.md` prior to this archive. Mechanically copied verbatim, empty `diff` readback. |
| `predefined-agent-governance` | Created | New capability — no existing `openspec/specs/predefined-agent-governance/spec.md` prior to this archive. Mechanically copied verbatim, empty `diff` readback. |

No existing spec under `openspec/specs/` overlapped with any of these three capabilities, so each copy is additive (no delta-merge logic was needed, the same situation `permission-model`'s archive pass found for `permission-hierarchy`).

## Archive Contents

- `proposal.md`: present
- `specs/agent-definition-locator/spec.md`, `specs/agent-registration-serving/spec.md`, `specs/predefined-agent-governance/spec.md`: present (the same three files now also at `openspec/specs/<name>/spec.md`, the new sources of truth)
- `design.md`: present (Architecture Decisions D1-D8)
- `exploration.md`: present
- `tasks.md`: present — all PR1a-PR7 tasks complete, 0 pending
- `verify-report.md`: present — 36/37 requirements PASS, 1 documented non-blocking test-coverage gap, 0 FAIL
- `state.yaml`: present, updated to `phase: archived` before the move

## Source of Truth Updated

- `openspec/specs/agent-definition-locator/spec.md`, `openspec/specs/agent-registration-serving/spec.md`, `openspec/specs/predefined-agent-governance/spec.md` now reflect the shipped `RoleLocator` three-kind resolution, the public `Agent` API, explicit `{id: Agent}` registration/serving, and Guard 1 predefined-role governance.

## Mechanical Copy / Move Evidence

- Delta-specs → main-specs copy: `cp` each of the three files into new `openspec/specs/<name>/` directories, `diff` (source vs. copy) → **empty, exit 0** for all three.
- Change-folder → archive move: pre-move recursive snapshot (`cp -R`) to a scratch directory, `git mv openspec/changes/library-first-agents openspec/changes/archive/2026-09-26-library-first-agents` (exit 0), confirmed source path absent, `diff -r` (snapshot vs. archived destination) → **empty, exit 0**. `archive-report.md` (this file) was written after that readback, consistent with the contract's additive-file exclusion.

## Verification Summary (from verify-report.md, same folder)

- Full suite: `pytest -q` → **2290 passed, 184 deselected, 17 xfailed**, 7 pre-existing/unrelated warnings.
- `ruff check .` → all checks passed.
- `ruff format --check .` → 410 files already formatted.
- `mypy src/` → no issues in 73 source files.
- 36/37 spec requirements traced independently to a covering test, verdict PASS.
- 1 documented, already-tracked, non-blocking test-coverage gap: Guard 1's per-role SemVer independence (`design.md` D8) has no dedicated regression test, though nothing in the shipped code conflates the two version spaces. Recommended for a follow-up GitHub issue; does not indicate broken behavior.
- 2 documented deviations: four requirements are covered by an existing test but not cited by exact title in any task's `Satisfies:` line (a citation gap in `tasks.md`, already self-identified by PR7-T3, independently re-confirmed here); "Predefined-role discovery stays scoped" is proven negatively (Guard 1 scoping tests + the full unchanged discovery-dependent regression suite) rather than through one dedicated positive-discovery test.

## SDD Cycle Complete

The change is archived. Implementation: **complete** — all ten PR slices (#68, #70, #85, #91, #92, #95, #98, #101, #103, #111) merged to `origin/main`. Verification: **complete**, 36/37 PASS, full suite/lint/format/type-check green.
Unfinished tasks: none. Unresolved findings: none blocking; one new-to-tracking-but-not-newly-discovered open follow-up noted above (Guard 1 SemVer-independence regression test) for separate GitHub issue filing (outside this SDD change's edit scope).
