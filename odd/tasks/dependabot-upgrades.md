# Dependabot dependency upgrades

## Tasks

- [x] Verify PR #3: structlog 25.5.0 to 26.1.0.
- [ ] Verify PR #2: mypy major upgrade.
- [ ] Verify PR #5: redis major upgrade.
- [ ] Verify PR #4: sentence-transformers major upgrade.

## Constraints

- Process one pull request at a time.
- Request Dependabot rebases and poll the head revision; never force-push.
- Regenerate `uv.lock` instead of hand-merging it.
- Do not merge pull requests.

## Evidence

- PR #3: structlog 26.1.0 installed in a worktree-local environment; the project requires Python 3.12, so the upstream removal of Python 3.8/3.9 support is inapplicable. No usages of the deprecated better-exceptions integration were found; the configured `BoundLogger`, contextvars processors, JSON renderer, and print logger factory remain supported.
- Checks: `tests/test_harness_logging.py` (4 passed), `ruff check .`, `ruff format --check .`, `mypy src/`, and `pytest -q` (2,285 passed, 184 deselected, 17 xfailed) passed under structlog 26.1.0.
