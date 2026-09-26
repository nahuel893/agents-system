# Issue #65 — Reject unregistered Permission class grants

## Scope
Ensure an unregistered `Permission` class supplied to `build_runtime` or `create_app(grants=...)` is rejected during build or application boot, while preserving R4 and grant-ceiling behavior.

## Tasks
- [x] Establish existing behavior and add focused regression tests (RED observed: both tests failed because no exception was raised).
- [x] Validate class grants through the existing registry lookup (GREEN: focused runtime and boot tests pass).
- [x] Update affected architecture documentation in English and Spanish.
- [~] Run all required checks, commit, rebase, publish PR, and confirm CI.

## Evidence
- RED: focused factory and application-boot tests each failed because no `UnknownPermissionNameError` was raised.
- GREEN: `pytest -q tests/test_harness_factory.py tests/test_main.py` — 127 passed.
- Implementation commit: `09b8945` (`fix(permissions): reject unregistered class grants`).
- Required checks: `ruff check .`, `ruff format --check .`, and `mypy src/` passed; full `pytest -q` passed with 2262 passed, 17 xfailed, and 184 deselected after rebasing on `origin/main`.
