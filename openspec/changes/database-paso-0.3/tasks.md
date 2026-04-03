# Paso 0.3 — PostgreSQL + pgvector Setup

## Tasks

- [x] Create `src/badie/models/base.py` — async engine, session factory, DeclarativeBase
- [x] Create `src/badie/models/tables.py` — ORM models (clients, orders, order_items, conversation_logs, catalog_embeddings)
- [x] Configure `Vector(512)` with HNSW index on catalog_embeddings
- [x] Update `src/badie/models/__init__.py` — re-export all models and utilities
- [x] Create `scripts/init_db.py` — pgvector extension + table creation script
- [x] Create `tests/test_models.py` — 7 structural tests (no DB required)
- [x] Validate: `ruff check` passes
- [x] Validate: `pytest tests/test_models.py` — 7/7 passed
- [x] Validate: all 5 table names importable from `badie.models.Base.metadata`
