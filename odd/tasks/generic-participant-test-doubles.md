# #70-B6 — Generic participant and conversation recorder test doubles

## Goal

Decouple test/app fixture wiring from client-bound services (`agentsys.services.clients` and `agentsys.services.conversation_log`), establishing deterministic test doubles in `tests/conftest.py` so a subsequent PR (#70-B7) can purely delete `services/clients.py` and `services/conversation_log.py` without breaking test suites or moving client behavior into `src/`.

## Decisions

- `tests/conftest.py` exports `FakeParticipant`, `FakeParticipantDirectory`, `FakeConversationRecorder`, and `fake_normalize_address`. None of them import `agentsys.services.clients`, `agentsys.services.conversation_log`, or Client ORM models (`agentsys.models.tables.Client`).
- `create_test_app()` defaults to `FakeParticipantDirectory()` and `FakeConversationRecorder()` via `create_app`'s public injection ports while preserving caller overrides.
- `tests/test_webhook.py` migrated away from `Client`, `ClientDirectory`, `ConversationLogRecorder`, and `normalize_phone`, using `FakeParticipant`, `FakeDirectory` with `fake_normalize_address`, and preserving all happy/inactive/unknown/malformed/recorder behavior.
- New contract tests in `tests/test_conftest_contract.py` prove fixture assembly no longer loads client service modules, verify structural protocol conformance, and prove `create_test_app` defaults and overrides work.
- Strict TDD verified: RED on baseline, GREEN after implementation.
- Legacy dedicated unit tests (`test_client_lookup.py`, `test_conversation_log.py`, `test_phone_normalization.py`) remain untouched as direct subjects for the B7 deletion slice.
- Zero changes to `src/` or any other paths outside allowed edit surfaces.

## Tasks

- [x] Add tests-only deterministic fakes in `tests/conftest.py` for `Participant`, `ParticipantDirectory`, and `ConversationRecorder` protocols (`FakeParticipant`, `FakeParticipantDirectory`, `FakeConversationRecorder`, `fake_normalize_address`) with no imports of client service modules or Client ORM models.
- [x] Wire `create_test_app()` defaults to those fakes via public injection ports with explicit caller overrides intact.
- [x] Migrate `tests/test_webhook.py` to use test fakes and generic fake normalization instead of `Client`, `ClientDirectory`, `ConversationLogRecorder`, or `normalize_phone`.
- [x] Add contract tests in `tests/test_conftest_contract.py` asserting no client service module leak, protocol conformance, and default/override state storage.
- [x] Execute Strict TDD cycle: RED captured on baseline, GREEN captured on implementation.
- [x] Run focused validation (`test_conftest_contract`, `test_webhook`, `test_main`, `test_openai_adapter`, `test_health`) and full test/ruff/mypy suites.
- [x] Document B6 completion and B7 prerequisite boundaries.

## Boundaries

- No modifications or deletions under `src/`, `scripts/`, `demo/`, `migrations/`, `pyproject.toml`, or workflows.
- No modifications to legacy dedicated unit tests (`test_client_lookup.py`, `test_conversation_log.py`, `test_phone_normalization.py`).
- Allowed files ONLY: `tests/conftest.py`, `tests/test_webhook.py`, `tests/test_conftest_contract.py`, `odd/tasks/generic-participant-test-doubles.md`.

## B6 Evidence & Remaining B7 Prerequisite

- **B6 Completed**:
  - Deterministic fakes added in `tests/conftest.py`:
    - `FakeParticipant`: implements `Participant` protocol with `id`, `active`, `name`, `phone_number`.
    - `fake_normalize_address`: generic test normalizer rejecting empty/non-phone strings with `ValueError` and formatting to `+<digits>` without regional quirks.
    - `FakeParticipantDirectory`: implements `ParticipantDirectory` with in-memory lookup, resolution spy/callable, and generic normalizer.
    - `FakeConversationRecorder`: implements `ConversationRecorder` with in-memory `turns` recording and optional `spy` support.
  - `create_test_app()` updated to default to `FakeParticipantDirectory()` and `FakeConversationRecorder()` without importing `agentsys.services.clients` or `agentsys.services.conversation_log`.
  - `tests/test_webhook.py` updated to replace `Client(...)` with `FakeParticipant(...)`, use `fake_normalize_address`, test wired test doubles in `test_the_wired_implementations_satisfy_the_ports`, and remove all imports of client service modules and ORM tables.
  - `tests/test_conftest_contract.py` extended with contract tests verifying isolation from client services, structural protocol conformance, default/override wiring, and fake behavior.
- **Remaining B7 Slice**:
  - Deletion of `src/agentsys/services/clients.py` and `src/agentsys/services/conversation_log.py`.
  - Deletion of legacy dedicated tests (`tests/test_client_lookup.py`, `tests/test_conversation_log.py`, `tests/test_phone_normalization.py`).
