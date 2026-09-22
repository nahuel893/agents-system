# #70-B5 — Remove seed data vertical

## Goal

Remove client-specific demo seed data service (`src/agentsys/services/seed_data.py`), entrypoint CLI script (`scripts/seed_demo_data.py`), and associated unit tests (`tests/test_seed_data.py`) without unstaging parent deletions, deleting additional code, or touching preserved models, services, connectors, or demo SQL assets:

- `src/agentsys/services/seed_data.py` deleted.
- `scripts/seed_demo_data.py` deleted.
- `tests/test_seed_data.py` deleted.
- Pure removal of the seed data vertical.
- Review-budget exception: 715 deletions across 3 files (documented #70 review-budget exception up to 732 lines); all deletions, indivisible vertical slice.
- `models/tables.py` (Client, Order, OrderItem, ConversationLog, CatalogEmbedding), models exports and tests, `services/clients.py`, `services/conversation_log.py`, `integration/webhook.py`, `tests/conftest.py`, #39 RBAC, demo assets (`demo/company/03_seed.sql`), CI (`.github/workflows/ci.yml`), config (`src/agentsys/config.py`), and docs (historical docs deliberately deferred) remain untouched and strictly preserved.

## Boundaries

- Do NOT delete any file/function/test/block, unstage parent deletes, commit, push, merge, or alter Git config.
- Do not recreate or move any client-domain behavior under `src/`.
- Intended boundary is pure removal of the seed data vertical.
- Strictly preserve `models/tables.py` and exports, `clients.py`, `conversation_log.py`, tests/conftest, webhook, #39, demo assets, CI, config, and docs.
- Production source files under `src/` remain unmodified in this validation phase.
- Only create/update `odd/tasks/remove-seed-data.md`.

## Tasks

- [x] Parent stages deletion of `src/agentsys/services/seed_data.py`, `scripts/seed_demo_data.py`, and `tests/test_seed_data.py`.
- [x] Verify zero active importers of `seed_data` or `seed_demo_data` across repository source code and tests.
- [x] Verify unaffected focused tests (`test_rag`, `test_models`, `test_config`, `test_embeddings`, `test_webhook`, `test_phone_normalization`) continue passing.
- [x] Verify preserved surfaces: `models/tables.py` and exports, `clients.py`, `conversation_log.py`, `tests/conftest.py`, webhook, #39, demo assets, CI, and config.
- [x] Run static analysis (Ruff check, Mypy on `src`) and full test suite.
- [x] Document metrics, review-budget exception, environmental findings, and test count delta (-18 from 790 to 772).

## Review Budget Exception

- **Target Files**: 3 files (`scripts/seed_demo_data.py`, `src/agentsys/services/seed_data.py`, `tests/test_seed_data.py`).
- **Deletion Lines**: 715 lines deleted via `git diff --numstat` (52 in `scripts/seed_demo_data.py`, 419 in `src/agentsys/services/seed_data.py`, 244 in `tests/test_seed_data.py`), corresponding to the documented #70 review-budget exception (~732 lines).
- **Indivisibility**: The service, CLI script, and test suite form an indivisible vertical. Splitting would leave dead imports or broken tests. Because all changes are deletions with zero additions or modifications, reviewer cognitive load is minimal.

## Evidence

- **Active Imports Check**: 0 active Python imports of `seed_data` or `seed_demo_data` across the repository.
  - Remaining occurrences in `src/` are ordinary word usages ("seed source", "could seed another survivor") in `embeddings.py` and `operator.py`.
  - Remaining occurrences in `tests/` are:
    - Intentional client-domain isolation subprocess probe in `tests/test_rag.py` (`test_rag_module_does_not_import_client_domain`), which explicitly asserts that importing `agentsys.services.rag` does not pull `agentsys.services.seed_data`.
    - Integration tests against `demo/company/03_seed.sql` in `tests/test_sales_reports_integration.py` and platform-owned fixture seeding in `tests/test_reports_integration.py`.
  - Historical doc references in `openspec/` and `docs/operations/dev-environment-security.md` are deliberately deferred per issue #70 scope.
- **Focused Unaffected Tests**:
  - `uv run pytest -q tests/test_rag.py`: 13 passed in 1.20s (including `test_rag_module_does_not_import_client_domain`).
  - `uv run pytest -q tests/test_models.py tests/test_config.py tests/test_embeddings.py tests/test_webhook.py tests/test_phone_normalization.py`: 108 passed, 3 xfailed, 3 warnings in 6.24s (xfails are pre-existing expected environmental marks: 2 in `test_config.py`, 1 in `test_webhook.py`).
  - Platform connectors & #39 tests: `uv run pytest -q tests/test_platform_tools_integration.py tests/test_platform_registries.py tests/test_platform_connectors.py tests/test_order_writer_connector.py`: 118 passed in 2.66s.
- **Static Analysis**:
  - `uv run ruff check .`: All checks passed!
  - `uv run mypy src`: Success: no issues found in 53 source files (decreased from 54 after removing `seed_data.py`).
  - `uv run mypy src tests/test_phone_normalization.py tests/test_rag.py`: Success: no issues found in 55 source files.
- **Full Test Suite & Environmental Finding**:
  - `uv run pytest -q`: 772 passed, 42 deselected, 19 xfailed, 5 warnings in 19.20s.
  - **Delta**: exactly -18 passing tests from 790 to 772, matching the 18 tests deleted with `tests/test_seed_data.py`.
  - **Environmental Note**: When a local Redis instance is running at `redis://localhost:6379/0`, running `test_post_text_message` writes `dedup:wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf` with a 300-second TTL. Because that specific test does not mock Redis unlike the other 22 tests in `test_webhook.py`, running the suite again within 5 minutes without clearing the key triggers deduplication (`webhook.duplicate_skipped`). Clearing the test key or running in a clean environment produces the exact deterministic 772 passed count.
- **Preserved Surfaces Verification**:
  - `src/agentsys/models/tables.py` and `src/agentsys/models/__init__.py` re-exports (`Client`, `Order`, `OrderItem`, `ConversationLog`, `CatalogEmbedding`, `AuditEvent`, `Base`).
  - `src/agentsys/services/clients.py` (`normalize_phone`, `normalize_argentine_mobile`, `lookup_or_create_client`, `ClientDirectory`).
  - `src/agentsys/services/conversation_log.py` (`log_conversation_turn`, `ConversationRecorder`).
  - `src/agentsys/integration/webhook.py` (`receive_message`, Meta HMAC validation).
  - `tests/conftest.py`, demo assets (`demo/company/03_seed.sql`), CI workflows (`.github/workflows/ci.yml`), config (`src/agentsys/config.py`).
