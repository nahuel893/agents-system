---
name: run-app
description: "Trigger: run the app, bring up infra, or smoke-test agents-system end to end (WhatsApp/chat/RAG). Load before starting services, running the suite against real infra, or reproducing a runtime bug."
license: Apache-2.0
metadata:
  author: agents-system
  version: "1.0"
---

## Infra (Postgres + Redis via Docker)

```bash
docker compose up -d          # postgres (postgres:16, db=agents_system) + redis (redis:8-alpine)
docker compose ps             # wait for postgres healthcheck = healthy
uv sync --group dev           # install deps into .venv (uv manages the venv)
uv run alembic upgrade head   # create tables
```

`uv` lives in `~/.local/bin` on this machine — if `uv` is not found, add it to PATH.

## Test suite (Strict TDD — always green before you move on)

```bash
uv run pytest                 # full suite; integration tests are deselected by default
uv run ruff check src/agents_system tests
uv run mypy src/agents_system
```

CI (`.github/workflows/ci.yml`) runs the same three on every pull_request.

## Local model (small, for smoke tests)

The chat/agent path needs a chat model. For local smoke, use Ollama with a small model:

```bash
ollama serve &                # if not already running
ollama pull qwen2.5:3b        # small; hallucinates products — smoke only, not eval
```

Point the adapter provider at `ollama` (config default). Note: `qwen2.5:3b` is unreliable
as a sales agent (documented) — use it to prove wiring, not answer quality.

## End-to-end smokes

None of `scripts/smoke.py`, `scripts/smoke_rag.py`, `scripts/smoke_chat.py` or
`scripts/chat.py` exist anymore — `scripts/` today only holds
`embed_catalog.py` (an unimplemented stub — do not run it expecting it to do
anything), `fake_openai_compatible_server.py`, `preflight_local_embeddings.sh`,
and `provision_bi_readonly.sql`. `agents_system` is a library now (`deployments/`
ships empty by design — see `deployments/README.md`), so there is no bundled
catalog to embed and no bundled client to run a smoke against. What actually
exercises each path today:

| Proves | Current command |
|--------|------------------|
| Base app / infra wiring | "Minimal smoke boot" below |
| Full agent turn, real model | `pytest -m live` |
| Catalog search (lexical, demo data) | "Catalog search (lexical, demo data)" below |
| Catalog search (real vector RAG) | no generic path — see note below |
| Interactive chat | `POST /v1/chat/completions`, see "Serving the API" |

### Minimal smoke boot (infra wiring only, no roles/tools)

`agents_system.main` has no module-level `app` — it exports the
`create_app(registry_factory=...)` factory a real caller supplies its own
tools to (`docs/platform/library-usage.md`). This boots one with an empty
registry: enough to prove Postgres/Redis/FastAPI wiring, not an agent turn.

```bash
cat > /tmp/smoke_app.py <<'EOF'
from agents_system.harness.registry import ToolRegistry
from agents_system.main import create_app

app = create_app(registry_factory=lambda *a, **k: ToolRegistry())
EOF
ALLOW_INSECURE=true uv run uvicorn smoke_app:app --app-dir /tmp --reload
curl localhost:8000/health   # {"status":"ok","postgres":"ok","redis":"ok"}
```

### Full agent turn against a real model

```bash
ollama pull qwen2.5:3b   # once, see "Local model" above
pytest -m live -q
```

Runs the sales-agent's own scenario end to end against real Ollama —
`docs/platform/live-eval.md`. Only `sales-agent` has a scenario today;
auditing the other three roles the same way is ADR-002 E.18, tracked by #52.

### Catalog search (lexical, demo data — not vector search)

```bash
docker exec agents-system-postgres-1 psql -U postgres -c 'CREATE DATABASE agents_system_demo;'
DEMO_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agents_system_demo \
  uv run python demo/load_demo_company.py
```

Loads a deterministic fake company (`demo/README.md`). Wire
`ReferenceBackends.catalog_search_tool_spec()`
(`docs/platform/reference-backends.md`) against it to exercise
`catalog_search` — lexical SKU/description matching only, `similarity` is
always `null`. There is no generic real vector-RAG smoke: the platform's own
pgvector-backed catalog was removed in #98, and `CatalogSource` (the vector
search a role's `catalog_search` actually calls, `src/agents_system/services/rag.py`)
is a client-injected port with no platform-shipped implementation — proving
it end to end needs a real deployment's own catalog, out of scope here (same
ADR-002 E.18 / #52 tracking as above).

## Serving the API

```bash
# See "Minimal smoke boot" above for a runnable app object; a real
# deployment builds its own `create_app(registry_factory=...)` entrypoint.
# GET /health           -> {postgres, redis} status
# POST /webhook         -> WhatsApp inbound (HMAC-signed)
# GET  /v1/models       -> OpenAI-compatible model list (Open WebUI)
# POST /v1/chat/completions
```

## Secrets

`.env` / `.env.example` are edited by the human only (protected). Never print credentials.
`.env` overrides `config.py` defaults — verify effective settings with
`uv run python -c "from agents_system.config import get_settings; print(get_settings())"`.
