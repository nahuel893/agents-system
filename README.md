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
- [Run the Demo API](#run-the-demo-api)
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
│   └── acme/                    # Example: a regional beverage distributor
│       └── sales-agent/          # ACME-specific sales agent
│           └── skills/           # Injected context
│
├── src/agents_system/             # Platform source
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
├── docker-compose.yml            # PostgreSQL + Redis
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

# Start infrastructure (PostgreSQL + Redis)
docker compose up -d

# Run tests to verify
uv run pytest
```

### Running

```bash
# Development
uv run uvicorn agents_system.main:app --reload --port 8000

# Production
uv run uvicorn agents_system.main:app --host 0.0.0.0 --port 8000 --workers 4
```

The platform exposes an **OpenAI-compatible adapter** at `/v1/*` — any OpenAI SDK client can point to it and get agent responses. This lets you use the platform as a drop-in replacement for OpenAI with your own roles and tools.

```bash
# Health check — also reports webhook-worker liveness and the outbox
# backlog ("webhook_worker": {"running": ..., "outbox_pending": ..., "outbox_leased": ...}),
# degraded when work is pending and no worker is running to drain it (#141)
curl http://localhost:8000/health

# Via the adapter (OpenAI-compatible)
#
# `ADAPTER_RUNTIMES` is empty by default -- the platform knows no deployment
# names -- so /v1 exposes nothing until you name one. Every named runtime
# also needs an explicit DEPLOY_GRANTS entry or boot refuses to start (issue
# #38 -- there is no auto-grant). Set both first:
#   ADAPTER_RUNTIMES='["acme__sales-agent"]'
#   DEPLOY_GRANTS='{"acme__sales-agent": ["read:catalog", "write:orders"]}'
#   ADAPTER_API_KEY=<something>      # required once a runtime is exposed
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "acme__sales-agent",
    "messages": [{"role": "user", "content": "Show me the catalog"}]
  }'
```

---

## Run the Demo API

Load the repeatable demo database first with
[`demo/load_demo_company.py`](demo/load_demo_company.py). Then configure the
demo URL, one `EVAL_PROVIDER` and its provider variables, optional
`DEMO_HOST`/`DEMO_PORT`, plus `ADAPTER_RUNTIMES` and `ADAPTER_API_KEY` to
publish a role:

```bash
export DEMO_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://your-compatible-endpoint.example/v1
export OPENAI_COMPATIBLE_MODEL=your-model-id
export OPENAI_COMPATIBLE_API_KEY="$YOUR_PROVIDER_API_KEY"
export DEMO_HOST=127.0.0.1
export DEMO_PORT=8000
export ADAPTER_RUNTIMES='["_generic__sales-agent"]'
export ADAPTER_API_KEY="$YOUR_DEMO_ADAPTER_API_KEY"

uv run python -m agents_system.demo
```

The entrypoint also boots through two fail-closed startup checks: a non-empty
`META_WEBHOOK_SECRET` (or `ALLOW_INSECURE=true`), and a genuinely read-only
`DEMO_DATABASE_URL` role. Both are explained, with the exact commands, in
[`docs/platform/demo-entrypoint.md`](docs/platform/demo-entrypoint.md).

`EVAL_PROVIDER` can also be `ollama`, `groq`, or `anthropic`; use that
provider's required variables instead. See the full walkthrough, provider
requirements, and `/v1` examples in
[`docs/platform/demo-entrypoint.md`](docs/platform/demo-entrypoint.md).

---

## Configuration

Environment variables (loaded from `.env`). Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://localhost:5432/agents_system` | PostgreSQL connection. Composed from `DB_USER`/`DB_HOST`/`DB_NAME` when those are set |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `OPENAI_API_KEY` | — | OpenAI API key — **embeddings only** (see `OPENAI_COMPATIBLE_API_KEY` for chat) |
| `ADAPTER_PROVIDER` | `ollama` | LLM provider: `ollama`, `groq`, `anthropic`, `openai_compatible` |
| `ADAPTER_RUNTIMES` | `[]` | Which runtimes `/v1` publishes. Empty publishes none — the runtime *cache* may hold more, for other channels, and those are never exposed here. Setting any requires `ADAPTER_API_KEY` |
| `WHATSAPP_RUNTIME_ID` | — | Which runtime inbound WhatsApp routes to, as `{deployment}__{role}`. Unset means the route answers 200 and runs no turn. **If `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` are both set, this must resolve to a runtime or the app refuses to boot** (#141) — otherwise `/webhook` durably accepts signed messages nothing ever processes |
| `DEPLOY_GRANTS` | `{}` | **Required** per configured runtime id (issue #38). JSON object mapping each `{deployment}__{role}` id to the list of permission wire names actually granted to it — e.g. `{"acme__sales-agent": ["read:catalog", "write:orders"]}`. A role's own manifest only declares what it *may* need; this is what a deployment actually *grants*, and it bounds Layer-2 revalidation for the whole life of that runtime. A configured runtime with no matching entry fails boot loudly, naming the runtime id |
| `EMBEDDING_PROVIDER` | `local` | Embedding provider: `local` or `openai` |
| `OPENAI_COMPATIBLE_BASE_URL` | — | **Required** for `openai_compatible`. Chat endpoint base URL |
| `OPENAI_COMPATIBLE_MODEL` | — | **Required** for `openai_compatible`. Model id to request |
| `OPENAI_COMPATIBLE_API_KEY` | — | Optional — omit it for keyless local endpoints |

Full reference: [`src/agents_system/config.py`](src/agents_system/config.py).

### Using any OpenAI-compatible endpoint

Set `ADAPTER_PROVIDER=openai_compatible` to point the agent at any service that
speaks the OpenAI chat API — a hosted provider, or a local server such as vLLM,
LM Studio or llama.cpp:

```bash
ADAPTER_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_BASE_URL=https://api.minimax.io/v1
OPENAI_COMPATIBLE_MODEL=MiniMax-M2.7
OPENAI_COMPATIBLE_API_KEY=your-key-here
```

Notes:

- `OPENAI_COMPATIBLE_API_KEY` is deliberately **separate** from `OPENAI_API_KEY`.
  The latter is the embeddings credential, so sharing one variable would stop you
  running embeddings on OpenAI and chat on a different host at the same time.
- `BASE_URL` and `MODEL` are required and the app fails at startup naming the
  missing variable. Without that check the client would quietly fall back to
  OpenAI's own API — wrong vendor, wrong credential, confusing auth error.
- The API key is optional because keyless local endpoints are common. When it is
  absent the app logs `openai_compatible.no_api_key` at startup, so a genuinely
  forgotten key is still visible.
- Reasoning models (MiniMax, and others that emit `<think>...</think>` inline in
  the response) are sanitized automatically — see
  [`src/agents_system/agent/reasoning.py`](src/agents_system/agent/reasoning.py). Streaming
  is **not** sanitized; nothing in the app streams today.

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
uv run pytest --cov=agents_system

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
| Library Usage (integrating `agents_system` into your app) | [`docs/platform/library-usage.md`](docs/platform/library-usage.md) | EN |
| Especificación de Plataforma | [`docs/platform_es/`](docs/platform_es/) | ES |
| Delivery & Delegation Protocol | [`docs/delivery/`](docs/delivery/) | EN + ES |
| Product Requirements (ACME) | [`PRD_WhatsApp_Sales_Agent.md`](PRD_WhatsApp_Sales_Agent.md) | ES |
| Delegation Work Ledger | [`delegations.md`](delegations.md) | EN |

---

## License

[MIT](LICENSE) © 2026 — Agents System Contributors.

*Built originally for a regional beverage distributor, as the first client deployment of the platform.*
