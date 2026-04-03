# Paso 0.2 — Pydantic Settings Configuration

## Tasks

- [x] Create `src/badie/config.py` with `Settings(BaseSettings)` and `get_settings()` singleton
- [x] Wire settings into `src/badie/main.py` (app factory + /health endpoint)
- [x] Sync `.env.example` with all Settings fields (added DEBUG, LOG_LEVEL)
- [x] Create `.env` from `.env.example` for local dev
- [x] Verify `.env` is in `.gitignore`
- [x] Create `tests/test_config.py` with default-loading and singleton tests
- [x] Validate: `ruff check src/` passes
- [x] Validate: `pytest tests/test_config.py -v` passes (2/2)
- [x] Validate: `get_settings().environment` returns "development"
