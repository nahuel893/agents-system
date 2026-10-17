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
- [x] T2 — Diagnose the coverage drop per file (route: delegated read-only explorer; evidence exceeds the inline budget).
- [x] T3 — Measure coverage correctly again, then add unit tests until the `ci` job reaches at least 94% (route: delegated writer; test config plus 2+ new test files).
- [ ] T4 — Confirm a green CI run on `main` and re-apply branch protection with the required checks `ci`, `secret-scan`, `dependency-audit`, `predefined-role-governance-marker` (route: inline).

## Acceptance criteria

- `pip-audit --local` over the locked set reports 0 flagged packages.
- The `ci` job passes with `--cov-fail-under=94` unchanged.
- All 14 CI jobs are green on `main`.
- Branch protection is back on `main`.

## Progress

- T1: `uv lock --upgrade-package fsspec --upgrade-package virtualenv` → fsspec 2026.9.0, virtualenv 21.14.5. `pip-audit --local`: 139 audited, 0 flagged, exit 0. Offline checks: `ruff check .` passed, `ruff format --check .` 612 files formatted, `mypy src/` no issues in 105 files, `pytest -q` 3878 passed, 258 deselected, 17 xfailed. Lockfile-only change of transitive packages: exempt from live scenarios. Merged as PR #306 (40eb734); its CI run 38986166540 has `dependency-audit` green.
- T2: most of the drop is a measurement artifact. The `filterwarnings` entry `error::agents_system.harness.loader.PredefinedRoleDeprecationWarning` in `pyproject.toml` (added with #222/#231) makes pytest import `agents_system` while it parses its config, before `pytest-cov` starts, so about 310 module-level lines read as missed (`harness/loader.py` 232 missed, `harness/registry.py` 41). With that entry neutralized the suite measures 93.73% locally (CI: 10464 statements; 94% needs 9837 covered). The rest is new code from #261-#278: five `*_postgres.py` services are only exercised by integration-marked tests (structurally outside the unit job), and the unit-testable gaps are led by `schedules.py` (27 missed, #274) and `main.py` (23 missed, 14 new). The last green baseline (1c62921, 2026-09-27) measured 95.86% locally.
- T3: filter approach (a). `filterwarnings` now matches the warning message with the stdlib `DeprecationWarning` category (`error:Predefined role .* is deprecated and will be removed:DeprecationWarning`), so pytest no longer imports `agents_system` while parsing its config. Proofs: `harness/registry.py` back to 100% and `harness/loader.py` to 98%; a temporary test calling `resolve("sales-agent")` without `pytest.warns` failed with `PredefinedRoleDeprecationWarning` raised as an error, then was deleted. `tests/test_reference_agents_in_live_tests.py` now asserts the new filter shape and that its regex matches the message the loader emits. New unit tests cover `schedules.py` (27 to 3 missed), `main.py` (23 to 14), `proactive.py`, `channels/whatsapp.py` and `agent/models.py`. Result: `pytest --cov=agents_system --cov-fail-under=94` reports 94.31% total, 3944 passed, 258 deselected, 17 xfailed; `ruff check .`, `ruff format --check .` and `mypy src/` clean. Unreachable by design: `schedules.py:213` (`AssertionError` after the weekday loop).
