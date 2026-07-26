# Agent System — Multi-Agent Runtime Platform

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-00a393)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-purple)](https://langchain-ai.github.io/langgraph/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17+-336791)](https://postgresql.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license)

**agents-system** is a production-grade runtime platform for instantiating and orchestrating AI agents. It provides a declarative agent-definition system, a pluggable harness for tool registration and permission enforcement, and a client-deployment model that lets you inject custom skills and context per organization — all without modifying the core runtime.

> 🇪🇸 Documentación en español disponible en [`docs/platform_es/`](docs/platform_es/).

---

## Table of Contents

- [Concept](#concept)
- [Architecture](#architecture)
  - [Declarative Agent Definitions](#declarative-agent-definitions)
  - [Harness — Three Enforcement Layers](#harness--three-enforcement-layers)
  - [Two-Layer Deployment Model](#two-layer-deployment-model)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Running](#running)
- [Configuration](#configuration)
- [Defining a New Agent Role](#defining-a-new-agent-role)
- [Creating a Client Deployment](#creating-a-client-deployment)
- [Multi-Agent Delegation Model](#multi-agent-delegation-model)
- [Testing](#testing)
- [Docs](#docs)
- [License](#license)

---

## Concept

The platform treats agents as **declarative, composable units**. You define:

- **What** the agent is (`role.md` — system prompt, purpose, scope)
- **What tools** it can use (`manifest.md` — tool list, permissions, skills)
- **How** it behaves (`policy.md` — autonomy, escalation rules, delegation policy)

These definitions live in two layers:

1. **Generic roles** under `platform/roles/{role}/` — reusable agent archetypes (orchestrator, data agent, sales agent, summary agent, etc.)
2. **Client deployments** under `deployments/{client}/{role}/` — per-client overrides that inherit, restrict, or extend the generic role

The **harness** (loader + registry + injector + interceptor + factory) assembles a validated, permission-bound `EquippedRuntime` from these declarations. The runtime is then handed to an agent orchestrator (LangGraph by default) for execution.

This design means you can instantiate a **data analyst agent**, a **sales agent**, an **accountant assistant**, or any other role — each with its own tool surface, permissions, and injected context — without writing new infrastructure code.

---

## Architecture

```
                          ┌─────────────────┐
                          │  OpenAI-compat.  │
                          │  Adapter (/v1)   │
                          └────────┬────────┘
                                   │
┌──────────────────────────────────────────────────────┐
│                   Agent Runtime                      │
│  LangGraph graph  (or any orchestrator)              │
│  Node: classify → route → execute → respond          │
│  State: Redis (checkpointer per thread_id)           │
└───────────────────────┬──────────────────────────────┘
                        │ injects
┌───────────────────────▼──────────────────────────────┐
│              EquippedRuntime (factory)                │
│  ┌──────────┬──────────┬──────────┬────────────────┐  │
│  │System    │ Tools    │ Denied   │ Skills         │  │
│  │Prompt    │ (granted)│ (audit)  │ (loaded .md)   │  │
│  └──────────┴──────────┴──────────┴────────────────┘  │
└───────────────────────┬──────────────────────────────┘
                        │ built from
┌───────────────────────▼──────────────────────────────┐
│               Harness (3 layers)                      │
│                                                       │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐  │
│  │ Registry │──▶│ Injector │──▶│  Interceptor     │  │
│  │ Tool     │   │ RBAC     │   │  Execution-time   │  │
│  │ Authority│   │ Surface  │   │  Enforcement      │  │
│  └──────────┘   └──────────┘   └──────────────────┘  │
│                                                       │
│  ┌────────────────────────────────────────────────┐   │
│  │ Loader                                        │   │
│  │  platform/roles/{role}/{role,manifest,policy} │   │
│  │  + deployments/{client}/{role}/ overrides     │   │
│  │  + YAML frontmatter merge directives          │   │
│  └────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────┘
```

### Declarative Agent Definitions

Each role is a folder with three Markdown files using YAML frontmatter:

```
platform/roles/{role}/
├── role.md       # System prompt, role purpose, scope
├── manifest.md   # Allowed tools, permissions, skills
└── policy.md     # Autonomy, escalation, delegation rules
```

Example (`platform/roles/sales-agent/role.md`):

```yaml
---
name: sales-agent
purpose: Assists customers with product selection and ordering
scope: sales
---
You are a sales assistant...
```

### Harness — Three Enforcement Layers

| Layer | Module | Responsibility |
|-------|--------|----------------|
| **1 — Registry** | `registry.py` | Authority on what tools exist. Register tools with required permissions. Fail loud on unknown tools. |
| **2 — Injector** | `injector.py` | RBAC surface resolution: `effective = role.permissions ∩ user.grants`. Reports denied tools with reasons. |
| **3 — Interceptor** | `interceptor.py` | Validates every tool call at execution time. Sensitive tools (write/send) are revalidated with current permissions. Raises `PolicyViolation` on blocks. |

The **factory** (`factory.py`) assembles everything into a frozen `EquippedRuntime`:
- Composed system prompt (role body + loaded skills)
- Granted tool surface
- Denied tools (for audit)
- Loaded skill modules

### Two-Layer Deployment Model

Generic roles live in `platform/roles/` and are reusable across clients. Client deployments live in `deployments/` and **inherit + restrict**:

```
deployments/{client}/{role}/
├── role.md        # Optional: override system prompt
├── manifest.md    # tools ⊆ parent.tools, permissions: inherit+
├── policy.md      # autonomy_rank ≤ parent.autonomy_rank
└── skills/        # Additional context injected into the prompt
    ├── contexto-cliente.md
    └── reglas-de-negocio.md
```

Merge rules:
- Absent field → inherited from parent
- `{inherit: true, add: [...]}` → parent list + additions
- `{inherit: true, remove: [...]}` → parent list minus removals
- `{override: value}` → replace parent (invariants still enforced)

Enforced invariants:
1. `tools_override ⊆ tools_parent`
2. `permissions_override ⊆ permissions_parent`
3. `autonomy_rank(override) ≤ autonomy_rank(parent)`
4. Execution limits in override ≤ parent/platform defaults

---

## Tech Stack

| Component | Technology | Why |
|-----------|------------|-----|
| **Runtime** | FastAPI (Python) | Async-native, high throughput, widely understood |
| **Agent Orchestration** | LangGraph 0.2+ | State graphs, checkpointing, multi-agent, `thread_id` isolation |
| **LLM** | Pluggable: Anthropic, OpenAI, Groq, Ollama | Model per task, none locked in |
| **Declarative Format** | YAML frontmatter in Markdown | Human-readable, diffable, composable |
| **Tool Registry** | Python dataclasses + permission tuples | Simple, testable, auditable |
| **Vector DB** | pgvector (PostgreSQL extension) | No extra dependency, good enough for catalog-scale |
| **State / Cache** | Redis 7+ | Sub-millisecond reads, TTL-based eviction |
| **Primary Database** | PostgreSQL 17 | Relational storage for orders, clients, audit logs |
| **Logging** | structlog | Structured JSON with correlation IDs |
| **Async Queue** | FastAPI BackgroundTasks → Celery (scale) | Start simple, scale when needed |

---

## Project Structure

```
├── platform/roles/               # Generic agent archetypes
│   ├── orchestrator/             # Top-level routing & policy
│   ├── sales-agent/              # Sales & ordering role
│   ├── data-agent/               # Data retrieval & synthesis
│   └── summary-agent/            # Meeting & conversation summaries
│
├── deployments/                  # Client-specific overrides
│   └── badie/                    # Example: Distribuidora BADIE S.A.
│       └── sales-agent/          # BADIE-specific sales agent
│           └── skills/           # Injected context
│
├── src/agentsys/                 # Platform source
│   ├── agent/                    # LangGraph agent graph & nodes
│   ├── connectors/               # External integrations (stubs, RAG)
│   ├── harness/                  # ⬅ Core platform
│   │   ├── loader.py             #   YAML definition loader + merge
│   │   ├── registry.py           #   Tool authority
│   │   ├── injector.py           #   RBAC surface resolution
│   │   ├── interceptor.py        #   Execution-time enforcement
│   │   └── factory.py            #   EquippedRuntime assembler
│   ├── integration/              # API adapters (OpenAI, webhook, WhatsApp)
│   ├── models/                   # SQLAlchemy ORM
│   ├── observability/            # Logging, middleware
│   ├── services/                 # Redis, embeddings, sync pipelines
│   ├── config.py                 # Pydantic-settings config
│   └── main.py                   # FastAPI application factory
│
├── tests/                        # 30+ test files (Strict TDD)
├── docs/                         # Architecture, platform, delivery (EN + ES)
├── docker-compose.yml            # pgvector + Redis
└── delegations.md                # Multi-agent work ledger
```

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (fast Python package manager)
- Docker & Docker Compose

### Setup

```bash
# Clone and enter
git clone https://github.com/your-org/agents-system.git
cd agents-system

# Create environment and install dependencies
uv sync

# Copy config and edit
cp .env.example .env
# Set at minimum: ANTHROPIC_API_KEY or OPENAI_API_KEY

# Start infrastructure (PostgreSQL + pgvector + Redis)
docker compose up -d

# Run tests to verify
uv run pytest
```

### Running

```bash
# Development
uv run uvicorn agentsys.main:app --reload --port 8000

# Production
uv run uvicorn agentsys.main:app --host 0.0.0.0 --port 8000 --workers 4
```

The platform exposes an **OpenAI-compatible adapter** at `/v1/*` — any OpenAI SDK client can point to it and get agent responses. This lets you use the platform as a drop-in replacement for OpenAI with your own roles and tools.

```bash
# Health check
curl http://localhost:8000/health

# Via the adapter (OpenAI-compatible)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "badie__sales-agent",
    "messages": [{"role": "user", "content": "Show me the catalog"}]
  }'
```

---

## Configuration

Environment variables (loaded from `.env`). Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://localhost:5432/badie` | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `ADAPTER_PROVIDER` | `ollama` | LLM provider: `ollama`, `groq`, `anthropic` |
| `ADAPTER_RUNTIMES` | `["badie__sales-agent"]` | Which runtimes to expose via `/v1` |
| `EMBEDDING_PROVIDER` | `local` | Embedding provider: `local` or `openai` |

Full reference: [`src/agentsys/config.py`](src/agentsys/config.py).

---

## Defining a New Agent Role

```bash
# 1. Create the role definition folder
mkdir -p platform/roles/my-analyst

# 2. Define role.md with YAML frontmatter + system prompt
cat > platform/roles/my-analyst/role.md << 'EOF'
---
name: my-analyst
purpose: Data analysis and reporting assistant
scope: analytics
---
You are a data analyst assistant. You help users query and visualize data.
EOF

# 3. Define manifest.md (tools + permissions + skills)
cat > platform/roles/my-analyst/manifest.md << 'EOF'
---
tools:
  - query-database
  - generate-chart
  - export-csv
permissions:
  - read:analytics
  - read:reports
skills: []
---
EOF

# 4. Define policy.md (autonomy, escalation)
cat > platform/roles/my-analyst/policy.md << 'EOF'
---
autonomy: supervised
escalation:
  max_retries: 3
  fallback_role: orchestrator
delegation: none
memory: session
---
EOF
```

Now register the required tools in code (`ToolRegistry`) and the new role is ready to use.

---

## Creating a Client Deployment

Override any generic role for a specific client with custom context:

```bash
mkdir -p deployments/acme-corp/my-analyst/skills

cat > deployments/acme-corp/my-analyst/role.md << 'EOF'
---
overrides: role.md
purpose: Data analysis for Acme Corp internal reporting
---
You are a data analyst for Acme Corp. You have access to their internal
databases and reporting tools. Always cite your data sources.
EOF

cat > deployments/acme-corp/my-analyst/skills/acme-glossary.md << 'EOF'
# Acme Corp Internal Glossary

- **ARPU**: Average Revenue Per User, calculated as...
- **Churn**: Customer who hasn't made a purchase in 90+ days...
EOF
```

The loader automatically merges the override with the generic role. Skills are appended to the system prompt as additional context.

---

## Multi-Agent Delegation Model

This repository itself is built by multiple AI agents working in parallel:

1. **Lead/Planner** writes task slices into `delegations.md` with scope, branch, and acceptance criteria.
2. **Workers** (Claude Code, Antigravity, OpenCode) each work in an **isolated git worktree + branch**.
3. **Coordination** happens through git + Engram (persistent memory) — no direct agent-to-agent communication.
4. **On finish**: workers commit → save to Engram → set status to `in_review` → notify the human integrator.

Full protocol: [`docs/delivery/delegation-protocol.md`](docs/delivery/delegation-protocol.md).

---

## Testing

The project uses **Strict TDD**: tests are written first (RED → GREEN).

```bash
# Run all tests
uv run pytest

# Unit tests only (skip integration)
uv run pytest -m 'not integration'

# With coverage
uv run pytest --cov=agentsys

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/
```

The test suite (30+ files) covers:
- Harness: loader, registry, injector, interceptor, factory
- Agent: graph, state, runtime nodes
- Integration: webhook, WhatsApp client, OpenAI adapter
- Services: Redis, embeddings, sync pipelines
- Connectors: RAG, stubs
- Models: SQLAlchemy ORM

---

## Docs

| Area | Path | Language |
|------|------|----------|
| Agent Platform Architecture | [`docs/architecture/`](docs/architecture/) | EN |
| Platform Specification | [`docs/platform/`](docs/platform/) | EN |
| Especificación de Plataforma | [`docs/platform_es/`](docs/platform_es/) | ES |
| Delivery & Delegation Protocol | [`docs/delivery/`](docs/delivery/) | EN + ES |
| Product Requirements (BADIE) | [`PRD_WhatsApp_Sales_Agent.md`](PRD_WhatsApp_Sales_Agent.md) | ES |
| Delegation Work Ledger | [`delegations.md`](delegations.md) | EN |

---

## License

[MIT](LICENSE) © 2026 — Agents System Contributors.

*Built originally for Distribuidora BADIE S.A. — Grupo Manzur, as the first client deployment of the platform.*
