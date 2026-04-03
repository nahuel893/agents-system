# Exploration: scaffold-paso-0.1

## Current State

The project root contains only planning artifacts — zero code exists yet:

```
agents-badie/
├── agent_system_architecture.svg   # Architecture diagram
├── PRD_WhatsApp_Sales_Agent.md     # Product requirements
└── openspec/                       # SDD artifacts
    ├── config.yaml
    ├── changes/arquitectura/       # Dev path document
    └── specs/
```

No `pyproject.toml`, no `src/`, no `.gitignore`. Clean slate.

## Target Folder Structure

Defined in `path-de-desarrollo.md` Paso 0.1. Uses `src/` layout with a `badie` package:

```
agents-badie/
├── pyproject.toml
├── .env.example
├── .gitignore
├── .python-version              # 3.12
├── src/
│   └── badie/
│       ├── __init__.py
│       ├── config.py            # pydantic-settings
│       ├── main.py              # FastAPI app factory
│       ├── integration/         # Layer 1: Webhooks, WhatsApp client
│       │   ├── __init__.py
│       │   ├── webhook.py
│       │   └── whatsapp_client.py
│       ├── agent/               # Layer 2: LangGraph orchestration
│       │   ├── __init__.py
│       │   ├── graph.py
│       │   ├── state.py
│       │   ├── nodes/
│       │   │   └── __init__.py
│       │   └── prompts/
│       │       └── __init__.py
│       ├── services/            # Business logic
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── orders.py
│       │   └── rag.py
│       ├── models/              # SQLAlchemy models
│       │   └── __init__.py
│       └── observability/       # structlog, middleware
│           └── __init__.py
├── tests/
│   ├── conftest.py
│   ├── conversations/           # JSON fixtures
│   ├── mocks/
│   └── payloads/                # Meta webhook payloads
└── scripts/
    └── embed_catalog.py
```

## Dependencies (with version constraints)

### Core (production)

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | `>=0.115,<1.0` | HTTP framework |
| `uvicorn[standard]` | `>=0.34` | ASGI server |
| `langgraph` | `>=0.2,<1.0` | Multi-agent orchestration |
| `langgraph-checkpoint-redis` | `>=0.1` | LangGraph state persistence |
| `langchain-anthropic` | `>=0.3,<1.0` | Claude API via LangChain |
| `langchain-openai` | `>=0.3,<1.0` | OpenAI embeddings |
| `sqlalchemy[asyncio]` | `>=2.0,<3.0` | Async ORM |
| `asyncpg` | `>=0.30` | PostgreSQL async driver |
| `pgvector` | `>=0.3` | pgvector Python extension |
| `redis` | `>=5.0,<6.0` | Redis client |
| `httpx` | `>=0.28` | Async HTTP client (WhatsApp API) |
| `pydantic-settings` | `>=2.0,<3.0` | Env config management |
| `structlog` | `>=24.0` | Structured logging |

### Dev (test + lint)

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | `>=8.0` | Test runner |
| `pytest-asyncio` | `>=0.24` | Async test support |
| `ruff` | `>=0.8` | Linter + formatter |
| `mypy` | `>=1.13` | Type checking |

## Key Decisions

1. **`src/` layout**: Prevents accidental imports from the project root. Industry standard for packages.
2. **Single package `badie`**: Not `agents_badie` — shorter imports: `from badie.agent.graph import ...`
3. **3-layer separation from day 1**: `integration/` (external world), `agent/` (LLM orchestration), `services/` (business logic) + `models/` (data). Matches the architecture SVG.
4. **All MVP deps upfront**: Even though Paso 0.1 only needs scaffold, we declare all deps now so `pip install -e ".[dev]"` gives a complete environment. No surprise additions later.
5. **`__init__.py` files are empty stubs**: Just namespace markers. No barrel exports until there's code to export.
6. **`.python-version` = `3.12`**: Locked to 3.12 for `pyenv` / `mise` compatibility.

## .env.example Variables

```env
# LLM
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Database
DATABASE_URL=postgresql+asyncpg://badie:badie@localhost:5432/badie

# Redis
REDIS_URL=redis://localhost:6379/0

# WhatsApp
WHATSAPP_TOKEN=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=

# App
LOG_LEVEL=DEBUG
ENVIRONMENT=development
```

## Done Criteria (from path-de-desarrollo.md)

- `pip install -e ".[dev]"` works without errors
- `ruff check .` passes
- `mypy src/` passes (empty modules)
- `pytest` runs (0 tests, 0 errors)

## Risks

- **LangGraph 0.2+ API churn**: Pin to `>=0.2,<1.0` to avoid breaking changes while allowing patches.
- **langchain-* version alignment**: langchain-anthropic and langchain-openai must be compatible versions. Both at `>=0.3,<1.0` should be fine.
- **pgvector requires system extension**: The Python package alone won't work without `CREATE EXTENSION vector` in PostgreSQL. Not a scaffold concern but noted for Paso 0.2.
