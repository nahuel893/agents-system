# #70-B4 — Remove article medallion sync vertical

## Goal

Remove article-specific medallion sync pipeline (`src/agentsys/services/sync_articles.py`), entrypoint CLI script (`scripts/sync_articles.py`), and associated unit tests (`tests/test_sync_articles.py`) without unstaging parent deletions, deleting additional code, or touching catalog embedding, models, or medallion base services:

- `src/agentsys/services/sync_articles.py` deleted.
- `scripts/sync_articles.py` deleted.
- `tests/test_sync_articles.py` deleted.
- Pure removal of the article medallion sync vertical.
- `models/tables.py` (including `CatalogEmbedding`), models exports and tests, `services/embeddings.py`, `services/medallion.py`, seed data, client services, and #39 RBAC remain untouched and strictly out of scope.

## Boundaries

- Do NOT delete any file/function/test/block, unstage parent deletes, commit, push, merge, or alter Git config.
- Do not recreate or move any client-domain behavior under `src/`.
- Intended boundary is pure removal of the article medallion sync vertical.
- Strictly preserve `models/tables.py` (including `CatalogEmbedding`), models exports/tests, `services/embeddings.py`, `services/medallion.py`, seed data, client services, and #39 RBAC.
- Production source files under `src/` remain unmodified in this repair/validation phase.
- Only create/update `odd/tasks/remove-article-sync.md`.

## Tasks

- [x] Parent stages deletion of `src/agentsys/services/sync_articles.py`, `scripts/sync_articles.py`, and `tests/test_sync_articles.py`.
- [x] Verify zero active importers of `sync_articles` across repository source code and tests.
- [x] Verify unaffected focused tests (`test_rag`, `test_models`, `test_config`, `test_embeddings`) continue passing.
- [x] Verify preserved surfaces: `models/tables.py` (`CatalogEmbedding`), `services/embeddings.py`, `services/medallion.py`, seed data, client services, and #39 RBAC.
- [x] Run static analysis (Ruff check, Mypy on `src`) and full test suite.
- [x] Confirm zero non-deletion repairs are required (all remaining references are purely docstring/prose or intentional RAG isolation guards).

## Evidence

- Active imports check: 0 active Python imports of `sync_articles` across the repository. Remaining occurrences are historical/ADR docs, tasks/specs, docstrings in `src/agentsys/services/medallion.py`, and the intentional client-domain isolation assertion in `tests/test_rag.py`.
- Focused unaffected tests: `uv run pytest -q tests/test_rag.py tests/test_models.py tests/test_config.py tests/test_embeddings.py` -> 66 passed, 2 xfailed in 2.79s (the 2 xfails in `test_config.py` are expected existing environment marks).
- Static gates:
  - `uv run ruff check .` -> All checks passed!
  - `uv run mypy src` -> Success: no issues found in 54 source files.
- Full test suite: `uv run pytest -q` -> 790 passed, 42 deselected, 19 xfailed, 4 warnings in 21.96s (delta: -4 tests from the removed `test_sync_articles.py`, passing count reduced from 794 to 790).
- Preserved surfaces verification:
  - `CatalogEmbedding` remains defined in `src/agentsys/models/tables.py` and exported in `src/agentsys/models/__init__.py`.
  - `uv run pytest -q tests/test_models.py tests/test_embeddings.py tests/test_seed_data.py tests/test_phone_normalization.py tests/test_client_lookup.py tests/test_platform_tools_integration.py tests/test_platform_registries.py tests/test_platform_connectors.py tests/test_order_writer_connector.py` -> 158 passed.
