# Proposal: Scaffold del Proyecto Python (Paso 0.1)

## Intent

Bootstrap agents-badie from zero code to a fully installable Python project. Every subsequent step (DB, webhooks, LangGraph agents) depends on this scaffold existing and passing basic tooling checks. Without it, nothing else can start.

## Scope

### In Scope
- `pyproject.toml` with all MVP dependencies (core + dev)
- `src/badie/` package with 3-layer folder structure (integration, agent, services, models, observability)
- `.gitignore`, `.env.example`, `.python-version` (3.12)
- Empty `__init__.py` in all packages
- Minimal `src/badie/main.py` — FastAPI app factory (importable, no routes)
- Minimal `tests/conftest.py` (empty, valid)
- `scripts/` directory with placeholder `embed_catalog.py`

### Out of Scope
- Any business logic, routes, or LLM integration
- Docker / docker-compose (deferred to Paso 0.2)
- Database migrations or models beyond empty `__init__.py`
- CI/CD pipeline configuration

## Approach

1. **Create `pyproject.toml`** using PEP 621 format with `[project.optional-dependencies] dev = [...]` for dev tooling. Build backend: `hatchling` with src-layout.
2. **Scaffold folder tree** — all directories with empty `__init__.py` files. Matches the 3-layer architecture from the PRD diagram.
3. **`main.py`** — single `create_app()` factory returning a `FastAPI()` instance. Module-level `app = create_app()` for uvicorn.
4. **Config files** — `.gitignore` (GitHub Python template), `.env.example` (all env vars from exploration), `.python-version` containing `3.12`.
5. **Validate** — run the 4 success criteria commands.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `pyproject.toml` | New | Project metadata, all dependencies |
| `src/badie/` | New | Full package tree (7 sub-packages) |
| `src/badie/main.py` | New | FastAPI app factory |
| `tests/conftest.py` | New | Empty pytest config |
| `.gitignore` | New | Python-specific ignores |
| `.env.example` | New | All required env vars documented |
| `.python-version` | New | Python 3.12 pin |
| `scripts/embed_catalog.py` | New | Placeholder script |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| LangGraph 0.2+ dependency conflict with langchain-* | Low | Pin ranges from exploration; validate with `pip install -e ".[dev]"` |
| pgvector Python package needs system-level PG extension | N/A for scaffold | Only relevant at Paso 0.2; no runtime code here |

## Rollback Plan

Delete all generated files: `pyproject.toml`, `src/`, `tests/`, `scripts/`, `.gitignore`, `.env.example`, `.python-version`. The project returns to its pre-scaffold state (only `openspec/` and docs remain). Single `git revert` if committed.

## Dependencies

- Python 3.12+ installed on the system
- `pip` capable of editable installs

## Success Criteria

- [ ] `pip install -e ".[dev]"` completes without errors
- [ ] `ruff check .` passes
- [ ] `pytest` runs with 0 tests, 0 errors
- [ ] `python -c "from badie.main import app"` succeeds
