# Design: Scaffold del Proyecto Python (Paso 0.1)

## Technical Approach

Bootstrap `agents-badie` as an installable Python package using src-layout, PEP 621 metadata, and hatchling build backend. All production and dev dependencies declared upfront so the environment is complete from day one. No business logic — only structure, wiring, and tooling validation.

## Architecture Decisions

| Decision | Choice | Alternatives | Rationale |
|----------|--------|-------------|-----------|
| **Package layout** | `src/badie/` (src-layout) | Flat layout (`badie/` at root) | Prevents accidental imports from project root during development. `pip install -e .` forces correct resolution through `site-packages`. Industry standard for publishable packages. |
| **Build backend** | `hatchling` | `setuptools`, `flit`, `pdm-backend` | Hatchling is PEP 621-native, zero-config for src-layout (`[tool.hatch.build.targets.wheel] packages = ["src/badie"]`). Setuptools requires extra `[tool.setuptools.packages.find]` config. Flit cannot handle src-layout without workarounds. |
| **Package name** | `badie` (not `agents_badie`) | `agents_badie`, `agentsbadie` | Shorter imports: `from badie.agent.graph import ...`. The repo name is the project identity; the package name is the developer interface. |
| **All deps upfront** | Declare all MVP deps in Paso 0.1 | Add deps incrementally per paso | One `pip install -e ".[dev]"` gives a complete environment. No surprise dependency additions in later pasos that could cause conflicts. Conflicts surface early. |
| **pytest-asyncio mode** | `auto` mode in `pyproject.toml` | `strict` mode (explicit markers) | The entire codebase is async (FastAPI, SQLAlchemy async, httpx). Auto mode removes boilerplate `@pytest.mark.asyncio` from every test. Set via `[tool.pytest.ini_options] asyncio_mode = "auto"`. |
| **Config pattern** | Pydantic Settings + `.env` | `python-dotenv` + dataclass, `dynaconf` | Already a dependency (`pydantic-settings`). Type-safe, validates at startup, integrates with FastAPI's `Depends()`. Design only — implementation is Paso 0.2. |

## Sub-Package to Architecture Layer Mapping

```
src/badie/
├── integration/      ── Layer 1: EXTERNAL BOUNDARY
│   (webhook.py, whatsapp_client.py)
│   Receives HTTP from Meta, sends messages via WhatsApp API.
│   Depends on: agent/, services/
│
├── agent/            ── Layer 2: LLM ORCHESTRATION
│   (graph.py, state.py, nodes/, prompts/)
│   LangGraph supervisor pattern. Stateless graph execution.
│   Depends on: services/
│
├── services/         ── Layer 3: BUSINESS LOGIC
│   (catalog.py, orders.py, rag.py)
│   Pure business rules. No framework imports.
│   Depends on: models/
│
├── models/           ── DATA (cross-cutting)
│   SQLAlchemy declarative models. No business logic.
│
└── observability/    ── INFRA (cross-cutting)
    structlog config, FastAPI middleware.
```

Dependency direction: `integration → agent → services → models`. Never reversed. `observability` is used by all layers.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `pyproject.toml` | Create | PEP 621 metadata, hatchling backend, all deps + `[dev]` extras, ruff/pytest/mypy config |
| `src/badie/__init__.py` | Create | Empty — namespace marker |
| `src/badie/main.py` | Create | `create_app() -> FastAPI` factory + module-level `app = create_app()` |
| `src/badie/{integration,agent,services,models,observability}/__init__.py` | Create | Empty stubs (7 sub-packages total including `agent/nodes/`, `agent/prompts/`) |
| `tests/conftest.py` | Create | Empty — valid pytest config, placeholder for future fixtures |
| `scripts/embed_catalog.py` | Create | Placeholder with docstring explaining future purpose |
| `.gitignore` | Create | GitHub Python template + `.env`, `*.egg-info`, `dist/` |
| `.env.example` | Create | All env vars documented with placeholder values |
| `.python-version` | Create | Contains `3.12` |

## Interfaces / Contracts

```python
# src/badie/main.py — the ONLY contract for Paso 0.1
from fastapi import FastAPI

def create_app() -> FastAPI:
    """Application factory. Returns bare FastAPI instance.
    Routes, middleware, and lifespan added in later pasos."""
    app = FastAPI(title="Badie", version="0.1.0")
    return app

app = create_app()  # uvicorn entry point: badie.main:app
```

## pyproject.toml Structure

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "badie"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [...]          # All 13 core deps from exploration

[project.optional-dependencies]
dev = [...]                   # pytest, pytest-asyncio, ruff, mypy

[tool.hatch.build.targets.wheel]
packages = ["src/badie"]

[tool.ruff]
src = ["src"]
target-version = "py312"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
python_version = "3.12"
strict = true
```

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Smoke | `create_app()` returns FastAPI instance | `conftest.py` fixture in Paso 0.2; for now, `python -c "from badie.main import app"` |
| Tooling | Ruff passes, mypy passes | Run `ruff check .` and `mypy src/` as validation commands |

No unit/integration/e2E tests in Paso 0.1 — there is no logic to test. `conftest.py` is created empty as infrastructure for future pasos.

## Migration / Rollout

No migration required. Greenfield scaffold. Rollback: `git revert` the single commit.

## Open Questions

None — all decisions are resolved from the exploration and proposal.
