# #70-B3 — Remove client medallion sync vertical

## Goal

Remove client-specific medallion sync pipeline (`src/agentsys/services/sync_clients.py`), entrypoint CLI script (`scripts/sync_clients.py`), and associated unit tests (`tests/test_sync_clients.py`) without unstaging parent deletions, deleting additional code, or touching client-domain lookup and normalization behavior:

- `src/agentsys/services/sync_clients.py` deleted.
- `scripts/sync_clients.py` deleted.
- `tests/test_sync_clients.py` deleted.
- Pure removal of the client medallion sync vertical.
- `src/agentsys/services/clients.py` (including `normalize_argentine_mobile`, `ClientDirectory`, lookup behavior), models/tables, migrations, medallion settings, client lookup and #39 RBAC remain untouched and strictly out of scope.

## Boundaries

- Do NOT delete any file/function/test/block, unstage parent deletes, commit, push, merge, or alter Git config.
- Do not recreate or move any client-domain behavior under `src/`.
- Intended boundary is pure removal of the client medallion sync vertical.
- `services/clients.py` (including `normalize_argentine_mobile`, `ClientDirectory`, lookup behavior), models/tables, migrations, medallion settings, client lookup and #39 RBAC are strictly out of scope.
- Production source files under `src/` remain unmodified in this repair/validation phase.
- Only edit `odd/tasks/remove-client-sync.md`.

## Tasks

- [x] Parent stages deletion of `src/agentsys/services/sync_clients.py`, `scripts/sync_clients.py`, and `tests/test_sync_clients.py`.
- [x] Verify zero active importers of `sync_clients` across repository source code and tests.
- [x] Verify unaffected phone normalization, client lookup, seed data, and RAG isolation tests continue passing.
- [x] Run static analysis (Ruff check, Mypy on `src`) and full test suite.
- [x] Confirm no non-deletion repairs are required (all remaining references are purely docstring/prose or intentional RAG isolation guards).

## Evidence

- `uv run pytest -q tests/test_phone_normalization.py tests/test_client_lookup.py tests/test_rag.py tests/test_seed_data.py tests/test_sync_articles.py` -> 54 passed.
- `uv run pytest -q` -> 794 passed, 42 deselected, 19 xfailed in 21.27s.
- `uv run ruff check .` -> All checks passed.
- `uv run mypy src tests/test_phone_normalization.py tests/test_rag.py` -> Success: no issues found in 57 source files.
- `grep -r "sync_clients"` -> 0 active Python imports in repository. Remaining occurrences are historical/ADR docs, comments/docstrings in `seed_data.py` and `medallion.py`, and the intentional client-domain isolation assertion in `tests/test_rag.py`.
