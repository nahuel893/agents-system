# Tasks: Scaffold del Proyecto Python (Paso 0.1)

## Phase 1: Infrastructure Files

- [x] 1.1 Create `.python-version` containing `3.12` — path: `.python-version`
- [x] 1.2 Create `.gitignore` with Python template (`.env`, `__pycache__/`, `*.egg-info/`, `dist/`, `.mypy_cache/`, `.ruff_cache/`, `.venv/`, `*.pyc`) — path: `.gitignore`
- [x] 1.3 Create `pyproject.toml` with PEP 621 metadata, hatchling backend (`packages = ["src/badie"]`), all 13 core deps, `[dev]` extras (pytest, pytest-asyncio, ruff, mypy), ruff/pytest/mypy tool config — path: `pyproject.toml`

## Phase 2: Package Structure

- [x] 2.1 Create `src/badie/__init__.py` (empty) — path: `src/badie/__init__.py`
- [x] 2.2 Create `src/badie/config.py` (empty placeholder) — path: `src/badie/config.py`
- [x] 2.3 Create integration sub-package: `src/badie/integration/__init__.py`, `webhook.py`, `whatsapp_client.py` (all empty)
- [x] 2.4 Create agent sub-package: `src/badie/agent/__init__.py`, `graph.py`, `state.py`, `nodes/__init__.py`, `prompts/__init__.py` (all empty)
- [x] 2.5 Create services sub-package: `src/badie/services/__init__.py`, `catalog.py`, `orders.py`, `rag.py` (all empty)
- [x] 2.6 Create models sub-package: `src/badie/models/__init__.py` (empty)
- [x] 2.7 Create observability sub-package: `src/badie/observability/__init__.py` (empty)

## Phase 3: Entry Points

- [x] 3.1 Create `src/badie/main.py` with `create_app() -> FastAPI` factory and module-level `app = create_app()` — path: `src/badie/main.py`
- [x] 3.2 Create `tests/conftest.py` (empty, valid Python) — path: `tests/conftest.py`
- [x] 3.3 Create `scripts/embed_catalog.py` (placeholder with docstring) — path: `scripts/embed_catalog.py`

## Phase 4: Config Files

- [x] 4.1 Create `.env.example` with all env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL`, `REDIS_URL`, `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `LOG_LEVEL`, `ENVIRONMENT` — path: `.env.example`

## Phase 5: Validation

- [x] 5.1 Run `pip install -e ".[dev]"` and verify exit code 0
- [x] 5.2 Run `ruff check .` and verify exit code 0
- [x] 5.3 Run `mypy src/` and verify exit code 0
- [x] 5.4 Run `pytest` and verify 0 tests collected, 0 errors
- [x] 5.5 Run `python -c "from badie.main import app"` and verify exit code 0
