# Finish issue #70 — platform boundary cleanup

## Goal
Complete the remaining package-boundary and documentation criteria for issue #70 on the existing `refactor/70-drop-pgvector` worktree, without touching the dirty primary worktree or creating a commit/PR.

## Reconciled tasks

- [x] Replace the remaining ACME-specific docstrings and consumer-facing package description; remove the stale WhatsApp-specific technology references from `openspec/config.yaml` as requested.
- [x] Add a default-suite test that builds the wheel in a temporary directory and asserts platform-owned anchors are present while known client-shaped paths are absent. Keep it out of the `integration` marker.
- [x] Update the English and Spanish tool/permission docs to describe the real `catalog_search` / injected `CatalogSource` contract and `read:catalog` permission, not a Postgres/pgvector-backed `rag_catalog_search` implementation.
- [x] Verify the full issue #70 acceptance criteria against this candidate, run relevant checks, and close #70 only if all criteria pass.

## Delivery

Moved off the stale `refactor/70-drop-pgvector` branch (pre-squash history) onto `refactor/70-close-leftovers` from `main`. Re-verified there: no ACME in `src/`; platform `description`; `openspec/config.yaml` clean; packaging test passes and fails when a `acme_*` file is planted under `src/`; full suite 793 passed; `ruff check .` and `mypy src/` clean. Delivered as a PR with `Closes #70`.

## Verification so far

- `uv run pytest tests/test_packaging_boundary.py -q`: 1 passed (wheel manifest checked).
- Full default suite: 746 passed, 18 xfailed, 43 deselected; `tests/test_webhook.py::test_post_text_message` was excluded to avoid modifying real Redis state per #82.
- `uv run ruff check src/agentsys/services/medallion.py src/agentsys/integration/openai_adapter.py tests/test_packaging_boundary.py`: passed.
- `uv run mypy src`: passed (50 source files).
- `git diff --check`: passed.
- Acceptance scans: no ACME in `src/agentsys`; no WhatsApp/pgvector in `openspec/config.yaml`; no stale RAG/pgvector arguments in the four requested docs.
- Blocked: after the user authorized installation, the Gentle AI binary installed, but managed-asset sync failed closed because the installed Pi settings and MCP configuration are symlinks. No symlink or user config was changed.
- User chose to leave this pending rather than modify the symlink-managed configuration. #70 remains open; native review and delivery are not complete.

## Scope

Worktree: the registered `wt-70-B9` worktree (`refactor/70-drop-pgvector`), stacked after merged PR #96.

Allowed source/documentation surfaces:
- `src/agentsys/services/medallion.py`
- `src/agentsys/integration/openai_adapter.py`
- `pyproject.toml`
- `openspec/config.yaml`
- `tests/test_packaging_boundary.py`
- `docs/platform/tool.md`
- `docs/platform_es/tool.md`
- `docs/architecture/permission-model.md`
- `docs/architecture_es/permission-model.md`

This task does not authorize edits to unrelated historical docs, the primary worktree, commits, pushes, or PR creation.
