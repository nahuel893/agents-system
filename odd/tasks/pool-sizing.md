# Pool sizing from settings

Issue: #11 — Pool sizing from settings, both engines

## Tasks

- [x] Add validated, per-engine pool settings and red tests for invalid values.
- [x] Wire operational, BI, medallion, and SQL-tool engine factories to configured pool options with red tests.
- [x] Document the capacity rule in English and Spanish, verify, commit, and open the PR.

## Evidence

- RED: `pytest -q tests/test_config.py tests/test_get_engine.py` failed during collection because `get_sql_engine` did not exist.
- Focused GREEN: `pytest -q tests/test_config.py tests/test_get_engine.py tests/test_main.py` passed: 171 passed, 2 xfailed.
- Rebased verification: `ruff check .`, `ruff format --check .`, and `mypy src/` passed. Full `pytest -q` passed: 2281 passed, 184 deselected, 17 xfailed.
- Commits: `cebe1b8` (implementation and tests); `a03a224` (documentation); `67fc28b` (verification record).
- PR #107 CI: all checks passed.
