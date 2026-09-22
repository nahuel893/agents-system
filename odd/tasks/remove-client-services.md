# #70-B7 — Remove final client-bound service layer

## Goal

Validate and document the pure deletion slice removing the final client-bound service layer and its dedicated unit tests:
- `src/agentsys/services/clients.py` deleted (-115 lines)
- `src/agentsys/services/conversation_log.py` deleted (-75 lines)
- `tests/test_client_lookup.py` deleted (-81 lines)
- `tests/test_conversation_log.py` deleted (-138 lines)
- `tests/test_phone_normalization.py` deleted (-94 lines)

Total staged deletion: 5 files changed, 503 deletions, 0 additions.

## Prerequisite: #70-B6

Slice #70-B6 (`odd/tasks/generic-participant-test-doubles.md`) decoupled test/app fixture wiring from `agentsys.services.clients` and `agentsys.services.conversation_log`:
- Added generic in-memory test doubles in `tests/conftest.py`: `FakeParticipant`, `FakeParticipantDirectory`, `FakeConversationRecorder`, and `fake_normalize_address`.
- Wired `create_test_app()` defaults to those test doubles via public injection ports without importing client services.
- Migrated `tests/test_webhook.py` away from `Client`, `ClientDirectory`, `ConversationLogRecorder`, and `normalize_phone`.
- Added contract tests in `tests/test_conftest_contract.py` asserting isolation from client service modules.
These test doubles enable B7 to cleanly drop the service modules and direct unit tests without breaking webhook integration or test suite execution.

## Review Budget Exception

This slice deletes 503 lines across 5 files, exceeding the standard 400-line review guideline.
**Exception Justification**: This is a pure deletion slice (503 deletions, 0 additions) with no behavioral modifications or new code. Deletions reduce maintenance surface and architectural coupling without adding cognitive review load.

## Boundaries & Scope Guards

- Pure deletion slice: No unstaging of parent deletions, no git commits/pushes/merges, no git config alterations.
- No recreation or movement of client-domain behavior under `src/`.
- Models tables (`src/agentsys/models/tables.py`, including `Client` and `ConversationLog` ORM tables) and models exports (`src/agentsys/models/__init__.py`) remain strictly untouched and are deliberately deferred to future schema cleanup slices.
- Preserved untouched: `tests/conftest.py`, `tests/test_webhook.py`, #39 RBAC, scripts, demo, migrations, config, docs.
- Only allowed write target: `odd/tasks/remove-client-services.md`.

## Tasks

- [x] Parent stages deletion of `src/agentsys/services/clients.py`, `src/agentsys/services/conversation_log.py`, `tests/test_client_lookup.py`, `tests/test_conversation_log.py`, and `tests/test_phone_normalization.py`.
- [x] Verify zero active imports or references to the deleted services across repository source and tests.
- [x] Verify B6 fake fixture contracts in `tests/test_conftest_contract.py` continue to pass.
- [x] Verify webhook behavior in `tests/test_webhook.py` and `create_app` isolation in `tests/test_main.py` remain green.
- [x] Verify models tables remain unchanged and deferred model tests in `tests/test_models.py` pass.
- [x] Run static analysis (`uv run ruff check .`, `uv run mypy src`).
- [x] Establish exact test count delta from B6 (777 passed) after deleting direct unit test suites, and verify full test suite matches.

## Evidence

### Active Reference Audit
- `grep -r "agentsys.services.clients"` / `grep -r "agentsys.services.conversation_log"`:
  - `src/`: 0 active imports. Only 1 docstring reference in `src/agentsys/services/participants.py` explaining historical decoupling.
  - `tests/`: 0 active imports. Occurrences in `test_conftest_contract.py`, `test_webhook.py`, and `test_rag.py` are intentional negative isolation assertions ensuring modules are not imported or leaked.
- `grep -r "lookup_or_create_client"` / `grep -r "normalize_phone"` / `grep -r "ClientDirectory"` / `grep -r "ConversationLogRecorder"`:
  - `src/`: 0 matches.
  - `tests/`: 0 active imports. Only historical docstrings/comments in `test_webhook.py` explaining prior architecture and local test variable names.

### Test Count Delta Analysis
- B6 baseline: **777 passed**
- Deleted direct unit test suites (-23 tests total):
  - `tests/test_client_lookup.py`: 5 tests
  - `tests/test_conversation_log.py`: 4 tests
  - `tests/test_phone_normalization.py`: 14 tests
- Expected test count: 777 - 23 = **754 passed**
- Full test suite run (`uv run pytest -q`):
  - `754 passed, 42 deselected, 19 xfailed, 1 warning in 15.84s`
  - Exact observed delta: **-23 passed tests**, exactly matching expectation.

### Focused Validation
- `uv run pytest -q tests/test_conftest_contract.py tests/test_webhook.py tests/test_main.py tests/test_models.py tests/test_config.py tests/test_rag.py`:
  - `140 passed, 3 xfailed in 7.02s`
  - `test_conftest_contract.py`: 7 passed (fixture isolation, fake protocol conformance, app wiring defaults/overrides)
  - `test_webhook.py`: 41 passed, 1 xfailed (webhook inbound routing, participant resolution, turn recording via fakes)
  - `test_main.py`: 38 passed (`create_app` port injection and app state isolation)
  - `test_models.py`: 9 passed (models tables unchanged and verified)
  - `test_config.py`: 32 passed, 2 xfailed (settings/environment markers preserved)
  - `test_rag.py`: 13 passed (RAG isolation guards preserved)

### Static Analysis
- `uv run ruff check .`: All checks passed!
- `uv run mypy src`: Success: no issues found in 51 source files.
