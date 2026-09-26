# Pool sizing from settings

Issue: #11 — Pool sizing from settings, both engines

## Tasks

- [x] Add validated, per-engine pool settings and red tests for invalid values.
- [x] Wire operational, BI, medallion, and SQL-tool engine factories to configured pool options with red tests.
- [x] Document the capacity rule in English and Spanish, verify, commit, and open the PR.
- [x] Add red tests for the restored operational ceiling and the turn-plus-worker capacity invariant.
- [x] Restore the default overflow, validate the capacity rule at settings startup, and correct the engine type annotation and admission comment.
- [ ] Update the ADR-001 worked examples in English and Spanish, then verify, rebase, push, update the PR body, and watch checks.

## Evidence

- RED: `pytest -q tests/test_config.py tests/test_get_engine.py` failed during collection because `get_sql_engine` did not exist.
- Focused GREEN: `pytest -q tests/test_config.py tests/test_get_engine.py tests/test_main.py` passed: 171 passed, 2 xfailed.
- Rebased verification: `ruff check .`, `ruff format --check .`, and `mypy src/` passed. Full `pytest -q` passed: 2281 passed, 184 deselected, 17 xfailed.
- Commits: `cebe1b8` (implementation and tests); `a03a224` (documentation); `67fc28b` (verification record).
- PR #107 CI: all checks passed.
- RED: `pytest -q tests/test_config.py tests/test_get_engine.py` failed with the old `database_max_overflow=0` default and no capacity validation; `mypy tests/test_get_engine.py` reported the incompatible `Settings` argument for `get_medallion_engine`.
- GREEN: `pytest -q tests/test_config.py tests/test_get_engine.py` passed: 83 passed, 2 xfailed. The focused mypy run no longer reports the medallion argument mismatch; it still reports five pre-existing `_env_file` constructor errors in the test file.
- Verification before push: `ruff check .`, `ruff format --check .`, and `mypy src/` passed. Full `pytest -q` passed after rebasing check: 2283 passed, 184 deselected, 17 xfailed.
- Commits: `e5b3a9d` (capacity validation, defaults, tests, and annotations); `74992e3` (bilingual ADR updates).
