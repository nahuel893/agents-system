# Redis Setup — Paso 0.4 Tasks

## Tasks

- [x] Create `src/badie/services/redis.py` — async connection pool singleton + client factory + shutdown helper
- [x] Update `src/badie/main.py` — add lifespan context manager that closes Redis pool on shutdown
- [x] Create `tests/test_redis.py` — unit tests for pool singleton, client creation (no running Redis needed)
- [x] Validate: `ruff check src/` passes
- [x] Validate: `pytest tests/test_redis.py -v` — 3/3 passed
- [x] Validate: import smoke test prints `<class 'redis.asyncio.client.Redis'>`

## Status

**Complete** — all tasks done, all validations green.
