# 305 — CI green on main after re-enabling Actions

Issue: #305 — CI is red on main after re-enabling Actions: coverage below 94 and two flagged dependencies.

## Objective

Bring every CI job on `main` back to green, then re-apply branch protection.

## Problem

Actions did not run from 2026-09-27 to 2026-10-16 while the repository was private. The first run after it went public (run 38445737709 on 7f30641) passes 12 of 14 jobs:

- `dependency-audit`: `fsspec 2026.4.0` (PYSEC-2026-4217) and `virtualenv 21.7.7` (PYSEC-2026-4011 to 4014) are flagged. Both are transitive; Dependabot PR #304 does not bump them.
- `ci`: coverage is 90.77% against `--cov-fail-under=94`.

## Constraints

- ADR-002 H.32: the coverage floor only ratchets up. Close the gap with unit tests, not a lower threshold.
- Branch protection stays off until `main` is green, or it blocks the fixes themselves.

## Tasks

- [x] T1 — Upgrade `fsspec` and `virtualenv` in `uv.lock` (route: inline; one mechanical lockfile change).
- [ ] T2 — Diagnose the coverage drop per file (route: delegated read-only explorer; evidence exceeds the inline budget).
- [ ] T3 — Add unit tests until the `ci` job reaches at least 94% (route: decided after T2).
- [ ] T4 — Confirm a green CI run on `main` and re-apply branch protection with the required checks `ci`, `secret-scan`, `dependency-audit`, `predefined-role-governance-marker` (route: inline).

## Acceptance criteria

- `pip-audit --local` over the locked set reports 0 flagged packages.
- The `ci` job passes with `--cov-fail-under=94` unchanged.
- All 14 CI jobs are green on `main`.
- Branch protection is back on `main`.

## Progress

- T1: `uv lock --upgrade-package fsspec --upgrade-package virtualenv` → fsspec 2026.9.0, virtualenv 21.14.5. `pip-audit --local`: 139 audited, 0 flagged, exit 0. Offline checks: `ruff check .` passed, `ruff format --check .` 612 files formatted, `mypy src/` no issues in 105 files, `pytest -q` 3878 passed, 258 deselected, 17 xfailed. Lockfile-only change of transitive packages: exempt from live scenarios.
