# #70-B2 — Remove catalog domain service

## Goal

Remove client-specific catalog domain persistence (`src/agentsys/services/catalog.py`) and its direct `CatalogTables` tests without relocating client domain code or breaking the generic RAG platform:

- `src/agentsys/services/catalog.py` is deleted.
- Direct `CatalogTables` unit tests in `tests/test_catalog_rag_connector.py` are removed.
- Generic RAG platform (`src/agentsys/services/rag.py` and `build_catalog_rag_connector` in `src/agentsys/connectors/rag_connector.py`) remains fully operational and decoupled via `CatalogSource` protocol.

## Boundaries

- Do not delete or mutate any generic RAG platform components (`services/rag.py`, `connectors/rag_connector.py`).
- Do not recreate, relocate, or stub client catalog domain storage under `src/`.
- No `CatalogTables` or `agentsys.services.catalog` references outside OpenSpec historical docs and intentional isolation test language in `tests/test_rag.py`.
- Production source files under `src/` remain unmodified in this repair/validation phase.

## Tasks

- [x] Parent removes `src/agentsys/services/catalog.py` and direct `CatalogTables` tests in `tests/test_catalog_rag_connector.py`.
- [x] Verify generic RAG platform tests (`tests/test_catalog_rag_connector.py` and `tests/test_rag.py`) pass without stale import errors.
- [x] Validate zero lingering `CatalogTables` or `agentsys.services.catalog` references in code outside intentional isolation checks in `tests/test_rag.py`.
- [x] Run static analysis (Ruff, Mypy) across modified and affected surfaces.

## Evidence

- `uv run pytest -q tests/test_catalog_rag_connector.py tests/test_rag.py` -> 20 passed.
- `uv run ruff check tests/test_catalog_rag_connector.py tests/test_rag.py src` -> All checks passed.
- `uv run mypy src/agentsys/services/rag.py src/agentsys/connectors/rag_connector.py tests/test_catalog_rag_connector.py tests/test_rag.py` -> Success: no issues found.
- `grep -r "CatalogTables"` -> 0 matches in repository.
- `grep -r "agentsys.services.catalog"` -> only intentional isolation assertions in `tests/test_rag.py`.
