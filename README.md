# Agent System — Multi-Agent Platform for WhatsApp Sales Automation

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-00a393)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-purple)](https://langchain-ai.github.io/langgraph/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17+-336791)](https://postgresql.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license)

**agents-system** is a production-grade multi-agent platform designed to automate WhatsApp-based sales conversations using LLMs. It began as a custom sales agent for [Distribuidora BADIE S.A.](https://www.badie.com/) (a major beverage distributor in Argentina) and evolved into a reusable agent orchestration platform.

> 🇪🇸 Documentación en español disponible en [`docs/platform_es/`](docs/platform_es/) (translated from the English source in `docs/platform/`).

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Running](#running)
- [Configuration](#configuration)
- [Multi-Agent Delegation Model](#multi-agent-delegation-model)
- [Testing](#testing)
- [Docs](#docs)
- [License](#license)

---

## What It Does

The system replaces the operational role of a **preventista** (field sales representative) in the daily order-taking process. Customers interact via WhatsApp in natural language, and the AI agent:

1. **Greets** the customer and identifies them.
2. **Presents** the product catalog (filtered by category on request).
3. **Interprets** orders in colloquial language — "dame dos cajones de la rubia" → correct SKU.
4. **Confirms** the order with a summary and saves it to the database.
5. **Allows modifications** after confirmation (until configurable cutoff time).
6. **Escalates** to a human when it cannot resolve the request.

All of this runs **24/7**, handles **1,000+ concurrent conversations**, and keeps LLM costs under control through prompt compression, model routing, and prompt caching.

---

## Architecture

The platform is split into two layers:

| Layer | Purpose |
|-------|---------|
| **Core Platform** | Reusable runtime, agent factory, tool registry, permission injector, execution interceptor, and declarative role definitions |
| **BADIE Delivery** | Concrete company implementation — WhatsApp integration, LangGraph sales agent, RAG product matching, sync pipelines |

### High-Level System View

```
┌─────────────────────────────────────────────────┐
│  Layer 1 — Integration                          │
│  WhatsApp Business API → Webhook FastAPI         │
│  → Message Router → Rate Limiter                │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  Layer 2 — LangGraph Orchestration              │
│  Supervisor Agent                               │
│  ├── Catalog Agent    (product query)            │
│  ├── Order Agent      (order interpretation)     │
│  ├── Confirm Agent    (order completion)         │
│  └── Modify Agent     (post-close modifications) │
│  State: Redis (per thread_id, TTL 24h)           │
└─────────────────────┬───────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────┐
│  Layer 3 — Data                                 │
│  pgvector (catalog embeddings)                  │
│  PostgreSQL (orders, clients, products)         │
│  Redis (conversational state)                   │
│  LLM API (Claude / GPT-4o mini / Groq)          │
└─────────────────────────────────────────────────┘
```

### Harness Architecture (Core Platform)

The platform enforces security and correctness through **three enforcement layers**:

1. **ToolRegistry** — The system's authority on what tools exist. Tools are registered with required permissions and executable connectors.
2. **Capability Injector** — Resolves which tools an agent gets based on `role.permissions ∩ user.grants`. Unknown tools → fail loud.
3. **Tool Call Interceptor** — Validates every call at execution time against the injected surface; sensitive tools (write/send) are revalidated with current permissions.

Agents are defined declaratively as folders with `role.md`, `manifest.md`, and `policy.md`, deployed per client.

---

## Tech Stack

| Component | Technology | Why |
|-----------|------------|-----|
| **WhatsApp Integration** | Meta Cloud API (WABA) | Official channel; BSP for high volume |
| **Backend / Webhook** | FastAPI (Python) | Async-native, high throughput, existing stack |
| **Agent Orchestration** | LangGraph 0.2+ | State-graph native, checkpointing, multi-agent |
| **LLM (conversation)** | Claude Sonnet 4 | Quality, tool use, prompt caching |
| **LLM (classification)** | Claude Haiku 4.5 | Cheap and fast for simple tasks |
| **LLM (adapter)** | OpenAI-compatible (Ollama, Groq, Anthropic) | Model flexibility per environment |
| **Embeddings** | text-embedding-3-small / BGE-M3 locally | Matryoshka dim reduction, open-source fallback |
| **Vector DB** | pgvector (PostgreSQL) | Reuses existing infra, no new dependency |
| **Conversational State** | Redis 7+ (TTL 24h) | Sub-millisecond, auto-eviction |
| **Primary Database** | PostgreSQL 17 | Orders, clients, catalog, embeddings |
| **Logging** | structlog | Structured JSON with correlation IDs |
| **Async Queue** | FastAPI BackgroundTasks → Celery (future) | MVP native → scale with dedicated workers |

---

## Project Structure

```
├── src/agentsys/              # Application source
│   ├── agent/                 # LangGraph agent graph and nodes
│   │   ├── graph.py           # Agent runtime orchestration
│   │   ├── nodes/             # Individual agent nodes
│   │   ├── prompts/           # System prompts per role
│   │   └── state.py           # Conversation state schema
│   ├── connectors/            # External integrations
│   │   ├── rag_connector.py   # RAG product matching
│   │   └── stubs.py           # BADIE connector stubs
│   ├── harness/               # Core platform runtime
│   │   ├── factory.py         # Agent runtime assembler
│   │   ├── injector.py        # Capability injection
│   │   ├── interceptor.py     # Execution-time enforcement
│   │   ├── loader.py          # Agent definition loader
│   │   └── registry.py        # Tool registry
│   ├── integration/           # WhatsApp / webhook / adapter
│   │   ├── openai_adapter.py  # OpenAI-compatible API layer
│   │   ├── webhook.py         # Meta webhook handler
│   │   ├── whatsapp_client.py # WhatsApp API client
│   │   └── meta_signature.py  # Webhook signature validation
│   ├── models/                # SQLAlchemy ORM models
│   ├── observability/         # Logging, middleware, metrics
│   ├── services/              # Redis, embeddings, sync
│   ├── config.py              # Pydantic-settings configuration
│   └── main.py                # FastAPI application factory
├── tests/                     # Comprehensive test suite (30+ files)
├── docs/                      # Architecture, platform, delivery docs (EN + ES)
│   ├── architecture/          # Architecture decisions, diagrams
│   ├── platform/              # Platform specification (EN)
│   ├── platform_es/           # Platform specification (ES)
│   └── delivery/              # Delivery & delegation protocol (EN + ES)
├── deployments/               # Client-specific agent deployments
├── scripts/                   # Utility scripts
├── docker-compose.yml         # PostgreSQL (pgvector) + Redis
├── pyproject.toml             # Dependencies, tool config
└── delegations.md             # Multi-agent task ledger
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (fast Python package manager)
- Docker & Docker Compose (for PostgreSQL + Redis)
- A Meta WhatsApp Business Account (for production use)

### Setup

```bash
# Clone the repository
git clone https://github.com/your-org/agents-system.git
cd agents-system

# Create environment and install dependencies
uv sync

# Copy environment file and configure
cp .env.example .env
# Edit .env with your API keys and settings

# Start infrastructure
docker compose up -d

# Run database migrations
uv run alembic upgrade head

# Verify it works
uv run pytest
```

### Running

```bash
# Development server with auto-reload
uv run uvicorn agentsys.main:app --reload --port 8000

# Production
uv run uvicorn agentsys.main:app --host 0.0.0.0 --port 8000 --workers 4
```

The health check endpoint confirms connectivity:

```bash
curl http://localhost:8000/health
# {"status":"ok","environment":"development","postgres":"ok","redis":"ok"}
```

---

## Configuration

The system is configured through environment variables (loaded from `.env`). Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://localhost:5432/badie` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key (for embeddings) |
| `ADAPTER_PROVIDER` | `ollama` | LLM provider (`ollama`, `groq`, `anthropic`) |
| `EMBEDDING_PROVIDER` | `local` | Embedding provider (`local` or `openai`) |
| `RAG_THRESHOLD_DIRECT` | `0.60` | Cosine similarity for direct match |
| `RAG_THRESHOLD_AMBIGUOUS` | `0.50` | Cosine similarity for ambiguous match |
| `WHATSAPP_PHONE_NUMBER_ID` | — | Meta WhatsApp phone number ID |
| `WHATSAPP_TOKEN` | — | Meta WhatsApp access token |

Full reference: [`src/agentsys/config.py`](src/agentsys/config.py).

---

## Multi-Agent Delegation Model

Multiple AI agents work on this repository **in parallel** through an asynchronous coordination protocol:

1. **Lead/Planner** writes task slices into `delegations.md` with scope, branch, and acceptance criteria.
2. **Workers** (Claude Code, Antigravity, OpenCode) each work in an **isolated git worktree + branch**.
3. **Coordination** happens through git + Engram (persistent memory) — no direct agent-to-agent communication.
4. **On finish**: workers commit → save to Engram → set status to `in_review` → notify the human integrator.

Full protocol: [`docs/delivery/delegation-protocol.md`](docs/delivery/delegation-protocol.md).

---

## Testing

The project follows **Strict TDD**: tests are written first (RED → GREEN).

```bash
# Run all tests (unit + integration)
uv run pytest

# Run only unit tests (skip integration, which require real services)
uv run pytest -m 'not integration'

# Run with coverage
uv run pytest --cov=agentsys

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/
```

The test suite covers:
- Harness components: registry, injector, interceptor, factory, loader
- Agent runtime and state management
- RAG connector and product matching
- Webhook handling and WhatsApp client
- OpenAI-compatible adapter layer
- Database models and migrations
- Redis connectivity and deduplication
- Configuration validation

---

## Docs

| Area | Path | Language |
|------|------|----------|
| Product Requirements | [`PRD_WhatsApp_Sales_Agent.md`](PRD_WhatsApp_Sales_Agent.md) | ES |
| Agent Platform Architecture | [`docs/architecture/`](docs/architecture/) | EN |
| Platform Specification | [`docs/platform/`](docs/platform/) | EN |
| Especificación de Plataforma | [`docs/platform_es/`](docs/platform_es/) | ES |
| Delivery & Delegation Protocol | [`docs/delivery/`](docs/delivery/) | EN + ES |
| Delegation Ledger | [`delegations.md`](delegations.md) | EN |

---

## License

[MIT](LICENSE) © 2026 — Agents System contributors.

Built originally for **Distribuidora BADIE S.A.** — Grupo Manzur.
