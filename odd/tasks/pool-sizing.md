# Pool sizing from settings

Issue: #11 — Pool sizing from settings, both engines

## Tasks

- [x] Add validated, per-engine pool settings and red tests for invalid values.
- [x] Wire operational, BI, medallion, and SQL-tool engine factories to configured pool options with red tests.
- [ ] Document the capacity rule in English and Spanish, verify, commit, and open the PR.

## Evidence

- RED: `pytest -q tests/test_config.py tests/test_get_engine.py` failed during collection because `get_sql_engine` did not exist.
- Focused GREEN: `pytest -q tests/test_config.py tests/test_get_engine.py tests/test_main.py` passed: 171 passed, 2 xfailed.
