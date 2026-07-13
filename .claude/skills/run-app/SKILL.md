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
docker compose up -d          # postgres (pgvector/pgvector:pg16, db=badie) + redis:7
docker compose ps             # wait for postgres healthcheck = healthy
uv sync --group dev           # install deps into .venv (uv manages the venv)
uv run python scripts/init_db.py   # create tables / pgvector extension
```

`uv` lives in `~/.local/bin` on this machine — if `uv` is not found, add it to PATH.

## Test suite (Strict TDD — always green before you move on)

```bash
uv run pytest                 # full suite; integration tests are deselected by default
uv run ruff check src/agentsys tests
uv run mypy src/agentsys
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

## End-to-end smokes (`scripts/`)

| Script | Proves |
|--------|--------|
| `scripts/smoke.py` | Base app / infra wiring |
| `scripts/smoke_rag.py` | Catalog vector search (BGE-M3 embeddings + pgvector) |
| `scripts/smoke_chat.py` | Full agent turn over the real catalog (needs Ollama + populated DB) |
| `scripts/chat.py` | Interactive REPL against the runtime |

Populate the catalog before RAG/chat smokes: `scripts/embed_catalog.py` / `scripts/sync_articles.py`.

## Serving the API

```bash
uv run uvicorn agentsys.main:app --reload
# GET /health           -> {postgres, redis} status
# POST /webhook         -> WhatsApp inbound (HMAC-signed)
# GET  /v1/models       -> OpenAI-compatible model list (Open WebUI)
# POST /v1/chat/completions
```

## Secrets

`.env` / `.env.example` are edited by the human only (protected). Never print credentials.
`.env` overrides `config.py` defaults — verify effective settings with
`uv run python -c "from agentsys.config import get_settings; print(get_settings())"`.
