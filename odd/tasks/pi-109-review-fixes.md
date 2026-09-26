# PI-109 review fixes

## Tasks

- [x] Add failing offline regression tests for concurrent provider-probe caching, total timeout, healthy readiness, and unlabeled audit-drop metrics.
- [x] Add a single-flight lock and total timeout to `ProviderHealthProbe`.
- [x] Update English and Spanish observability documentation for the bounded single-flight probe.
- [ ] Run required checks, commit, rebase, push, update PR #109, and wait for CI.

## Evidence

- RED: `pytest -q tests/test_health.py tests/test_observability_metrics.py` reported 2 expected failures: 100 cold-cache requests and no total timeout.
- GREEN: `pytest -q tests/test_health.py tests/test_observability_metrics.py` passed: 26 passed (5 pre-existing SQLAlchemy warnings).
