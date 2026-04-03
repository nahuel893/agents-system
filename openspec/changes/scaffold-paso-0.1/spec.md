# Scaffold Specification (Paso 0.1)

## Purpose

Define the exact structure, dependencies, and validation criteria for bootstrapping agents-badie as an installable Python project. This scaffold is the foundation for all subsequent steps.

## Requirements

### Requirement: Project File Structure

The scaffold MUST create the following directory tree under the project root using `src/` layout with a `badie` package:

| Path | Type | Content |
|------|------|---------|
| `pyproject.toml` | file | PEP 621 metadata (see Requirement: pyproject.toml) |
| `.python-version` | file | `3.12` |
| `.gitignore` | file | Python-specific ignores |
| `.env.example` | file | All env vars (see Requirement: Environment Config) |
| `src/badie/__init__.py` | file | Empty |
| `src/badie/config.py` | file | Empty placeholder |
| `src/badie/main.py` | file | FastAPI app factory (see Requirement: App Entry Point) |
| `src/badie/integration/__init__.py` | file | Empty |
| `src/badie/integration/webhook.py` | file | Empty |
| `src/badie/integration/whatsapp_client.py` | file | Empty |
| `src/badie/agent/__init__.py` | file | Empty |
| `src/badie/agent/graph.py` | file | Empty |
| `src/badie/agent/state.py` | file | Empty |
| `src/badie/agent/nodes/__init__.py` | file | Empty |
| `src/badie/agent/prompts/__init__.py` | file | Empty |
| `src/badie/services/__init__.py` | file | Empty |
| `src/badie/services/catalog.py` | file | Empty |
| `src/badie/services/orders.py` | file | Empty |
| `src/badie/services/rag.py` | file | Empty |
| `src/badie/models/__init__.py` | file | Empty |
| `src/badie/observability/__init__.py` | file | Empty |
| `tests/conftest.py` | file | Empty, valid Python |
| `scripts/embed_catalog.py` | file | Empty placeholder |

Every directory containing `.py` files MUST have an `__init__.py`.

#### Scenario: All directories and files exist after scaffold

- GIVEN the scaffold has been applied
- WHEN listing all paths in the table above
- THEN every path MUST exist and be a valid file

### Requirement: pyproject.toml Configuration

The project MUST use `hatchling` as build backend with `src/` layout. MUST declare `project.name = "badie"` and `requires-python = ">=3.12"`.

Core dependencies MUST include (with these minimum version ranges):

| Package | Version Range |
|---------|--------------|
| `fastapi` | `>=0.115,<1.0` |
| `uvicorn[standard]` | `>=0.34` |
| `langgraph` | `>=0.2,<1.0` |
| `langgraph-checkpoint-redis` | `>=0.1` |
| `langchain-anthropic` | `>=0.3,<1.0` |
| `langchain-openai` | `>=0.3,<1.0` |
| `sqlalchemy[asyncio]` | `>=2.0,<3.0` |
| `asyncpg` | `>=0.30` |
| `pgvector` | `>=0.3` |
| `redis` | `>=5.0,<6.0` |
| `httpx` | `>=0.28` |
| `pydantic-settings` | `>=2.0,<3.0` |
| `structlog` | `>=24.0` |

Dev dependencies MUST be declared under `[project.optional-dependencies] dev` and include:

| Package | Version Range |
|---------|--------------|
| `pytest` | `>=8.0` |
| `pytest-asyncio` | `>=0.24` |
| `ruff` | `>=0.8` |
| `mypy` | `>=1.13` |

#### Scenario: Editable install succeeds

- GIVEN a fresh clone with Python 3.12+
- WHEN running `pip install -e ".[dev]"`
- THEN the command MUST exit with code 0
- AND all core and dev dependencies MUST be installed

#### Scenario: Build backend is correctly configured

- GIVEN `pyproject.toml` exists
- WHEN inspecting `[build-system]`
- THEN `requires` MUST include `hatchling`
- AND `[tool.hatch.build.targets.wheel]` MUST set `packages = ["src/badie"]`

### Requirement: App Entry Point

`src/badie/main.py` MUST define a `create_app()` function that returns a `FastAPI` instance. MUST also define a module-level `app = create_app()` for uvicorn compatibility.

#### Scenario: App is importable

- GIVEN the project is installed via `pip install -e ".[dev]"`
- WHEN running `python -c "from badie.main import app"`
- THEN the command MUST exit with code 0
- AND `app` MUST be a `FastAPI` instance

### Requirement: Environment Configuration

`.env.example` MUST document all environment variables the system will need. MUST include at minimum:

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (LLM)
- `DATABASE_URL` (PostgreSQL async connection string)
- `REDIS_URL`
- `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`
- `LOG_LEVEL`, `ENVIRONMENT`

Each variable SHOULD include a placeholder or example value.

#### Scenario: All env vars documented

- GIVEN `.env.example` exists
- WHEN reading its contents
- THEN all variables listed above MUST be present

### Requirement: Linter and Type Checker Pass

The scaffold MUST pass `ruff check .` and `mypy src/` with zero errors on the empty codebase.

#### Scenario: Ruff passes on scaffold

- GIVEN the project is installed
- WHEN running `ruff check .`
- THEN the command MUST exit with code 0

#### Scenario: Mypy passes on scaffold

- GIVEN the project is installed
- WHEN running `mypy src/`
- THEN the command MUST exit with code 0

### Requirement: Test Runner Works

`pytest` MUST execute successfully on the scaffold, collecting 0 tests with 0 errors.

#### Scenario: Pytest runs clean

- GIVEN the project is installed
- WHEN running `pytest`
- THEN the command MUST exit with code 0
- AND output MUST indicate 0 tests collected (no errors or collection failures)
