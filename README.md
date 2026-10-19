# agents-system — a development kit for building AI agents

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-00a393)](https://fastapi.tiangolo.com)
[![LangGraph 1.x](https://img.shields.io/badge/LangGraph-1.x-purple)](https://langchain-ai.github.io/langgraph/)
[![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)](https://postgresql.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license)

**agents-system** is a development kit for building AI agents. You describe an agent (a system prompt, the tools it may call, the policy that bounds it), hand it your own tools, and the kit assembles a permission-bound runtime around it. The smallest path is an agent with one tool of your own in about ten lines: see the [Quickstart](docs/platform/library-usage.md#quickstart), which also covers installing the kit from another repository. For every feature in one place, with its entry point and the doc that explains it, see the [Feature reference](docs/platform/features.md).

Installing the package also installs a command, `agents` (coming in the next release; see [Command line](docs/platform/cli.md)). `agents check ./my-agent` shows what an agent resolves to: each declared tool equipped or denied, and why. `agents chat ./my-agent` talks to it in the terminal, `agents serve` runs the server, and `agents db upgrade` applies the database migrations from an installed package.

Everything else is an optional piece you turn on at runtime, only when you need it: an OpenAI-compatible **server**, **channels** such as WhatsApp, the eight **predefined roles** (reusable agent archetypes), and **reference backends** (Postgres, Redis, embeddings). A plain install is the light core that runs the Quickstart (about 76 MB); the server, the channels and the reference backends are extras you install when you use them (`agents-system[server]`, `[postgres]`, `[embeddings]`, ...), and the predefined roles ship with the core. Agent definitions can be declarative Markdown folders *and* plain Python; a two-layer deployment model lets your application inject its own tools, skills, and per-client overrides, all without modifying the core.

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
- [Library-First Usage](#library-first-usage)
- [Read-Only SQL Tool](#read-only-sql-tool)
- [Provider and Model per Agent](#provider-and-model-per-agent)
- [Defining a New Predefined Role](#defining-a-new-predefined-role)
- [Creating a Client Deployment](#creating-a-client-deployment)
- [Multi-Agent Delegation (Planned)](#multi-agent-delegation-planned)
- [Observability](#observability)
- [Production Deployment](#production-deployment)
- [Testing](#testing)
- [Governance: Guard 1](#governance-guard-1)
- [Docs](#docs)
- [License](#license)

---

## Concept

The kit treats agents as **declarative, composable units**. You define:

- **What** the agent is (`role.md` — system prompt, purpose, scope)
- **What tools** it can use (`manifest.md` — tool list, permissions, skills)
- **How** it behaves (`policy.md` — autonomy, escalation rules, delegation policy, model/provider)

These definitions live in two layers:

1. **Predefined roles** under `platform/roles/{role}/` — eight reusable agent archetypes the platform ships (`orchestrator`, `sales-agent`, `data-agent`, `summary-agent`, `accountant-agent`, `developer-agent`, `operator-agent`, `support-agent`), all extending the shared `agent`/`base` roles.
2. **Client deployment overrides** — per-client narrowing of a predefined role. These live in **your own application's repository**, not in this one; see [Creating a Client Deployment](#creating-a-client-deployment).

Since the library-first change (ADR-004), you are no longer limited to the eight predefined roles: build a custom `Agent` directly in Python — from a folder, from parameters, or both — that `extends` a predefined role (or the generic `agent` role), resolved through the exact same pipeline as a platform role. See [Library-First Usage](#library-first-usage).

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
│  State: Postgres (checkpointer per thread_id)        │
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

`deployments/{client}/{role}/` above is a path *shape*, resolved through an
injectable `RootConfig(deployments_root=...)` — in a real deployment it
almost always points at the **consuming application's own repository**, not
at this one's `deployments/` folder (which ships empty; see
[Creating a Client Deployment](#creating-a-client-deployment)).

### Declarative Agent Definitions

Each role is a folder with three Markdown files using YAML frontmatter:

```
platform/roles/{role}/
├── role.md       # System prompt, role purpose, scope
├── manifest.md   # Allowed tools, permissions, skills
└── policy.md     # Autonomy, escalation, delegation rules, model/provider
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

Predefined roles live in `platform/roles/` and ship inside the package. Client
deployment overrides **inherit + restrict** a predefined role for one client,
and live in the *consuming application's* own repository:

```
deployments/{client}/{role}/
├── role.md        # Optional: override system prompt
├── manifest.md    # tools ⊆ parent.tools, permissions: inherit+
├── policy.md      # autonomy_rank ≤ parent.autonomy_rank
└── skills/        # Additional context injected into the prompt
```

This repository's own `deployments/` directory ships **empty** — it exists
only as the documented mount-point contract
([`deployments/README.md`](deployments/README.md)) and as the default
`RootConfig` falls back to when nothing else is configured. Point
`RootConfig(deployments_root=...)` at wherever *your* deployment tree
actually lives; it is never packaged inside `agents_system`.

Merge rules:
- Absent field → inherited from parent
- `{inherit: true, add: [...]}` → parent list + additions
- `{inherit: true, remove: [...]}` → parent list minus removals
- `{override: value}` → replace parent (invariants still enforced)

Enforced invariants — additive for capability, subtractive for safety:
1. `tools_override ⊆ tools_parent`
2. `permissions_override ⊆ permissions_parent`
3. `autonomy_rank(override) ≤ autonomy_rank(parent)`
4. Execution limits in override ≤ parent/platform defaults

---

## Tech Stack

| Component | Technology | Why |
|-----------|------------|-----|
| **Runtime** | FastAPI (Python) | Async-native, high throughput, widely understood |
| **Agent Orchestration** | LangGraph 1.x | State graphs, checkpointing, multi-agent, `thread_id` isolation |
| **LLM** | Pluggable: Anthropic, OpenAI, Groq, Ollama | Model per task, none locked in |
| **Declarative Format** | YAML frontmatter in Markdown | Human-readable, diffable, composable |
| **Tool Registry** | Python dataclasses + permission tuples | Simple, testable, auditable |
| **Conversation state** | PostgreSQL 16 (LangGraph checkpointer) | Survives restarts; idle conversations purged after `CHECKPOINTER_RETENTION_S` ([conversation memory](docs/platform/conversation-memory.md)) |
| **Redis** | Redis 8+ | Reported by `GET /health`; it no longer holds conversation state |
| **Primary Database** | PostgreSQL 16 | Relational storage for orders, clients, audit logs |
| **Logging** | structlog | Structured JSON with correlation IDs |
| **Async Queue** | FastAPI BackgroundTasks → Celery (scale) | Start simple, scale when needed |

---

## Project Structure

```
├── platform/roles/               # Predefined agent archetypes (8, plus the shared `agent`/`base` roles)
│   ├── orchestrator/             # Top-level routing & policy
│   ├── sales-agent/              # Sales & ordering role
│   ├── data-agent/               # Data retrieval & synthesis
│   ├── summary-agent/            # Meeting & conversation summaries
│   ├── accountant-agent/
│   ├── developer-agent/
│   ├── operator-agent/
│   └── support-agent/
│
├── deployments/                  # Mount point for client deployment overrides.
│   └── README.md                 # Ships empty — see "Creating a Client Deployment"
│
├── examples/
│   └── demo/
│       └── app.py                 # Runnable demo entrypoint — see "Run the Demo API"
│
├── src/agents_system/             # Platform source
│   ├── agent/                    # LangGraph agent graph & the Python `Agent` API (agent/spec.py)
│   ├── connectors/               # External integrations (stubs, RAG, SQL tool, sandboxed operator)
│   ├── harness/                  # ⬅ Core platform
│   │   ├── loader.py             #   Definition loader + merge + RootConfig
│   │   ├── registry.py           #   Tool authority
│   │   ├── injector.py           #   RBAC surface resolution
│   │   ├── interceptor.py        #   Execution-time enforcement
│   │   └── factory.py            #   EquippedRuntime assembler
│   ├── integration/              # API adapters (OpenAI, webhook, WhatsApp)
│   ├── models/                   # SQLAlchemy ORM
│   ├── observability/            # Logging, metrics, middleware
│   ├── services/                 # Redis, embeddings, sync pipelines
│   ├── config.py                 # Pydantic-settings config
│   └── main.py                   # FastAPI application factory
│
├── tests/                        # Strict-TDD test suite (offline by default; opt-in live/load suites)
├── docs/                         # Architecture, platform, delivery, operations docs (EN + ES)
├── docker-compose.yml            # PostgreSQL + Redis (development)
├── docker-compose.prod.yml       # Production stack (postgres, redis, migrate, app)
└── deploy/systemd/                # Production systemd units
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
git clone https://github.com/nahuel893/agents-system.git
cd agents-system

# Create environment and install dependencies (every extra, through the
# dev group: see "Library-First Usage" for what a plain install brings)
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

The `agents` command (coming in the next release; see [Command line](docs/platform/cli.md)) does the same from an installed package, with no checkout:

```bash
agents db upgrade                      # apply the migrations (the postgres extra)
agents serve --host 0.0.0.0 --port 8000 --workers 4   # the served app (the server extra)
agents check ./my-agent --grant read:catalog          # what an agent resolves to
agents chat ./my-agent --provider anthropic           # talk to it in the terminal
```

The platform exposes an **OpenAI-compatible adapter** at `/v1/*` — any OpenAI SDK client can point to it and get agent responses. This lets you use the platform as a drop-in replacement for OpenAI with your own roles and tools.

```bash
# Health check — also reports webhook-worker liveness and the outbox
# backlog ("webhook_worker": {"running": ..., "outbox_pending": ..., "outbox_leased": ...}),
# degraded when work is pending and no worker is running to drain it (#141)
curl http://localhost:8000/health

# Via the adapter (OpenAI-compatible)
#
# Nothing is registered by default -- the platform knows no deployment
# names -- so /v1 exposes nothing until you register a runtime id and name
# it in ADAPTER_RUNTIMES. Every registered runtime also needs an explicit
# DEPLOY_GRANTS entry or boot refuses to start (issue #38 -- there is no
# auto-grant). The id is your choice; nothing parses it:
#   AGENT_REGISTRATIONS='{"sales": "folder:sales-agent"}'   # under AGENTS_ROOT
#   ADAPTER_RUNTIMES='["sales"]'
#   DEPLOY_GRANTS='{"sales": ["read:catalog", "write:orders"]}'
#   ADAPTER_API_KEY=<something>      # required once a runtime is exposed
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sales",
    "messages": [{"role": "user", "content": "Show me the catalog"}]
  }'
```

---

## Run the Demo API

Load the repeatable demo database first with
[`demo/load_demo_company.py`](demo/load_demo_company.py). Then configure the
demo URL, one `EVAL_PROVIDER` and its provider variables, optional
`DEMO_HOST`/`DEMO_PORT`, plus `ADAPTER_RUNTIMES` and `ADAPTER_API_KEY` to
publish the reference `sales-agent`, which the demo registers itself as
`demo-sales-agent`:

```bash
export DEMO_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://your-compatible-endpoint.example/v1
export OPENAI_COMPATIBLE_MODEL=your-model-id
export OPENAI_COMPATIBLE_API_KEY="$YOUR_PROVIDER_API_KEY"
export DEMO_HOST=127.0.0.1
export DEMO_PORT=8000
export ADAPTER_RUNTIMES='["demo-sales-agent"]'
export DEPLOY_GRANTS='{"demo-sales-agent": ["read:catalog", "read:client_registry", "write:orders", "write:order_items", "read:price_lists", "send:message"]}'
export ADAPTER_API_KEY="$YOUR_DEMO_ADAPTER_API_KEY"

uv run python examples/demo/app.py
```

`DEPLOY_GRANTS` is required for every registered runtime id (issue #38); the
example above grants `demo-sales-agent` its full declared permission set,
which is safe because none of it is a T3 (`exec:`/`run:`) permission and
`sales-agent` declares `untrusted_input: true`.

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
| `CHECKPOINTER_RETENTION_S` | `86400` | How long an idle conversation is kept before the purge deletes it; `never` keeps it indefinitely. See [conversation memory](docs/platform/conversation-memory.md) |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Anthropic chat model, used when the agent declares none |
| `OPENAI_API_KEY` | — | OpenAI API key — **embeddings only** (see `OPENAI_COMPATIBLE_API_KEY` for chat) |
| `ADAPTER_PROVIDER` | `ollama` | LLM provider: `ollama`, `groq`, `anthropic`, `openai_compatible` |
| `AGENT_REGISTRATIONS` | `{}` | Which runtimes the app builds when `create_app` gets no `agents=` (ADR-004). JSON object mapping each runtime id you choose to a predefined role, as `"{role}"` or `"{role}@{client}"` — e.g. `{"acme-sales": "sales-agent@acme"}`. A value may also be `"folder:<name>"`, an agent folder under `AGENTS_ROOT`. A malformed id or value fails boot, naming it. Replaces the removed `{deployment}__{role}` ids: see [migrating](docs/platform/deployment.md#migrating-from-the-old-runtime-ids) |
| `AGENTS_ROOT` | `""` | Absolute directory that `"folder:<name>"` values of `AGENT_REGISTRATIONS` resolve under. Unset, a folder value fails boot; so does one that escapes the root (absolute path, `..`, symlink out). See [deployment](docs/platform/deployment.md#serving-an-agent-folder-agents_root-coming-in-the-next-release) |
| `ADAPTER_RUNTIMES` | `[]` | Which registered runtime ids `/v1` publishes. Empty publishes none — the runtime *cache* may hold more, for other channels, and those are never exposed here. Setting any requires `ADAPTER_API_KEY` |
| `WHATSAPP_RUNTIME_ID` | — | Which registered runtime id inbound WhatsApp routes to. Unset means the route answers 200 and runs no turn. **If `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` are both set, this must resolve to a runtime or the app refuses to boot** (#141) — otherwise `/webhook` durably accepts signed messages nothing ever processes |
| `DEPLOY_GRANTS` | `{}` | **Required** per registered runtime id (issue #38). JSON object mapping each registered id to the list of permission wire names actually granted to it — e.g. `{"acme-sales": ["read:catalog", "write:orders"]}`. A role's own manifest only declares what it *may* need; this is what a deployment actually *grants*, and it bounds Layer-2 revalidation for the whole life of that runtime. A registered runtime with no matching entry fails boot loudly, naming the runtime id |
| `REGISTRY_FACTORY` | `default_registry_factory` | `module:callable` returning a `ToolRegistry`. **Required** whenever any agent is registered — see [Production Deployment](#production-deployment) |
| `MODEL_PRICES` | `{}` | Cost table for real per-turn token usage, keyed by provider model id — see [Provider and Model per Agent](#provider-and-model-per-agent) |
| `EMBEDDING_PROVIDER` | `local` | Embedding provider: `local` or `openai` |
| `OPENAI_COMPATIBLE_BASE_URL` | — | **Required** for `openai_compatible`. Chat endpoint base URL |
| `OPENAI_COMPATIBLE_MODEL` | — | **Required** for `openai_compatible`. Model id to request |
| `OPENAI_COMPATIBLE_API_KEY` | — | Optional — omit it for keyless local endpoints |
| `OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S` | `45.0` | Bounded per-HTTP-call timeout to the `openai_compatible` provider (issue #145). Keep it under a role's `total_execution_timeout_s` (60s default), and leave room for two calls per tool-using turn: `developer-agent` sets 120s for that reason (issue #172). Boot logs a WARNING for a role whose budget is below it |

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
- Per-call provider HTTP status and retries are visible on `/metrics`
  (`agent_provider_requests_total`, `agent_provider_retries_total`) and a
  stuck call now fails after `OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S` instead of
  waiting silently (issue #145) — see
  [`docs/platform/observability.md`](docs/platform/observability.md#provider-observability-and-the-issue-145-investigation)
  for the full investigation: a suspected provider rate limit turned out to
  be a live-eval harness setup gap (`catalog_search` querying an unseeded
  demo table), now fixed in `services/reference.py` and
  `tests/test_live_load_phase3.py`.

---

## Library-First Usage

`agents_system` is not only the runtime behind `/v1` — it is an importable
library. The package ships the harness and the eight predefined roles; it
never ships a client's tools or a client's deployment overrides. A consuming
application supplies both at runtime, by injection. Running a turn this way
needs no server environment: no `.env`, no `META_WEBHOOK_SECRET`, no
database — the runtime never reads the server's `Settings`.

For the smallest start, an agent with one tool of your own in about ten
lines, see the Quickstart in
[`docs/platform/library-usage.md`](docs/platform/library-usage.md#quickstart),
which also covers installing from another repository. A plain install is the
light core this needs; the served app, the database, the SQL tool, embeddings
and the chat model providers are extras (`agents-system[server]`, ...), listed
under "Extras" there.

This example runs exactly as written (`tests/test_readme_library_example.py`
executes it with a scripted model; `scripts/smoke-installed-wheel.sh` runs it
against the built wheel in an empty environment):

<!-- runnable-example: library -->
```python
import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage

import agents_system

# 1. Your own tools. A connector is YOUR code: it can call your database,
#    your APIs, anything. This one searches a small in-memory catalog.
CATALOG = [
    {"id": "item-001", "name": "Item Alpha", "stock": 50},
    {"id": "item-002", "name": "Item Beta", "stock": 0},
]


def catalog_search(inputs: dict, /) -> dict:
    query = inputs.get("query", "").lower()
    return {"results": [item for item in CATALOG if query in item["name"].lower()]}


registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=catalog_search,
        tier=agents_system.Tier.T1,
        description="Search the product catalog by free-text query.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
)


# 2. Register every other tool the role declares. sales-agent declares five;
#    build_runtime raises InjectionError for any declared tool the registry
#    lacks, except escalation_notifier (inherited from its parent role), which
#    gets a built-in default that reaches no human. A tool you register but do
#    not grant is denied at build time and never reaches the model, so a stub
#    is enough for the ones this app does not use.
def not_wired(inputs: dict, /) -> dict:
    return {"error": "this tool is not wired in this application"}


for name, permission, tier in [
    ("client_lookup", "read:client_registry", agents_system.Tier.T1),
    ("message_sender", "send:message", agents_system.Tier.T2),
    ("order_writer", "write:orders", agents_system.Tier.T2),
]:
    registry.register(
        agents_system.ToolSpec(
            name=name,
            required_permissions=(permission,),
            connector=not_wired,
            tier=tier,
        )
    )

# 3. Build the equipped runtime: a predefined role, your registry, and the
#    permissions this caller holds. Only granted tools are equipped.
equipped = agents_system.build_runtime(
    "sales-agent",
    registry,
    granted_permissions=["read:catalog"],
)


# 4. Turn it into a live agent with any LangChain chat model and run a turn.
async def main(model: BaseChatModel) -> list[AnyMessage]:
    agent = agents_system.AgentRuntime(equipped, model=model)
    return await agent.run_turn(
        [HumanMessage(content="Do you have Item Alpha in stock?")],
        session_id="demo-session",
    )


if __name__ == "__main__":
    from langchain_anthropic import ChatAnthropic

    messages = asyncio.run(main(ChatAnthropic(model="claude-sonnet-4-5")))
    print(messages[-1].content)
```

To narrow the role with a deployment override of your own, point
`RootConfig` at your deployment tree and name the client — `deployments_root`
is never guessed for you:

```python
equipped = agents_system.build_runtime(
    "sales-agent",
    registry,
    granted_permissions=["read:catalog"],
    client="my-client",
    roots=agents_system.RootConfig(deployments_root=MY_APP_ROOT / "deployments"),
)
```

**Cost and audit are opt-in in library use.** Pass
`model_prices={"claude-sonnet-4-5": agents_system.ModelPrice(input_per_million=3.0, output_per_million=15.0)}`
to `AgentRuntime` for per-turn cost; without prices, cost is `None`. To record
audit events, start an `agents_system.AuditSink` and register it with
`agents_system.AuditSink.set_current(sink)`; without one, the process logs a
single `audit.sink_not_registered` warning and records nothing. `create_app`
does both for the served app (from `MODEL_PRICES`, and with a sink on the
application database).

A custom agent can be defined directly in Python instead of (or as well as) a
folder, and `extends` a predefined role the same way a deployment override
would:

```python
from agents_system import Agent

triage_bot = Agent.from_folder(
    "agents/triage-bot",
    extends="sales-agent",  # inherit tools/policy from a predefined role
    skill_contents={
        "triage-notes": "..."
    },  # inline skill text, or a `skills/` folder next to it
)
```

Register runtimes with `create_app` — every registered id needs an explicit
`grants` entry, there is no auto-grant (issue #38):

```python
from agents_system.main import create_app

app = create_app(
    registry_factory=build_registry,
    agents={"acme-sales": "sales-agent", "triage-bot": triage_bot},
    clients={"acme-sales": "acme"},
    grants={
        "acme-sales": ["read:catalog", "write:orders"],
        "triage-bot": ["read:catalog"],
    },
)
```

`AGENT_REGISTRATIONS` (`{id: "role"}` or `{id: "role@client"}`) is the
environment-variable equivalent of `agents=` when `create_app` receives none.

**Importer safety ceiling.** `extends` is additive for capability — a child
keeps its parent's tools and permissions and may add its own — but
subtractive for safety: autonomy, execution limits, and audit redaction may
only match or tighten the parent's, never loosen them, at every hop of an
`extends:` chain, and even for a parentless importer root (held to the
platform defaults). Breaking this raises `DefinitionError`.

Full reference, the two traps, and the complete worked example:
[`docs/platform/library-usage.md`](docs/platform/library-usage.md).

---

## Read-Only SQL Tool

A database-enforced read-only SQL tool (`sql_query`, permission `query:sql`,
tier T2) ships as a registerable tool but is **not equipped by any predefined
role by default** — adding it to a shipped role is a deliberate,
SemVer-relevant decision left to each deployment. Three independent layers
enforce read-only access: a dedicated PostgreSQL role with
`default_transaction_read_only` and no DDL/DML grants; an application-side
guard that parses every query with `sqlglot` and executes only its own
canonical re-rendering of a single allowlisted `SELECT`; and a byte-capped,
error-sanitized tool surface that never leaks raw driver errors.

To use it: provision the `sql_readonly` role (`scripts/provision_sql_readonly.sql`),
build a dedicated engine, register
`build_sql_query_tool_spec(engine, SqlQueryConfig(views={...}))` with your
`ToolRegistry`, and grant `query:sql` through `DEPLOY_GRANTS` to the roles
that should have it.

Full design and provisioning steps:
[`docs/architecture/adr-007-read-only-sql-tool.md`](docs/architecture/adr-007-read-only-sql-tool.md).

---

## Provider and Model per Agent

Every runtime defaults to the single, global `ADAPTER_PROVIDER`-driven model.
A role's `policy.md` — a predefined role's own, a deployment override's, or
the `Agent`/`Agent.from_folder` Python API's `model=` parameter — can opt
into its own provider and model instead:

```yaml
model:
  provider: anthropic          # ollama | groq | anthropic | openai_compatible
  model: claude-3-5-sonnet-latest
  temperature: 0.2             # optional, 0-2
  max_tokens: 1024              # optional, positive integer
```

Declaring nothing keeps the global default. Role-to-role `extends:` folds
`model` per key (a child can declare only `temperature` and still inherit
`provider`/`model`); a deployment override replaces it wholesale. Unlike
autonomy or execution limits, there is no ceiling here — model/provider
selection never widens what a role is allowed to do.

**Cost.** Real per-turn token usage and cost are tracked automatically,
keyed by the resolved provider model id. Configure prices with `MODEL_PRICES`:

```bash
export MODEL_PRICES='{"claude-sonnet-4-5": {"input_per_million": 3.0, "output_per_million": 15.0}}'
```

A model id with no entry reports cost as `None` rather than guessing. In
library use (no `create_app`), pass the same table to
`AgentRuntime(model_prices=...)` instead — the runtime never reads
`MODEL_PRICES` itself.

Full reference: [`docs/platform/policy.md`](docs/platform/policy.md).

---

## Defining a New Predefined Role

This adds a **new predefined role to the platform itself** (this repository).
If you're integrating `agents_system` into your own application, use
[Library-First Usage](#library-first-usage) instead — you rarely need to fork
this repo to get a custom agent.

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

Changing tools or permissions on an **existing** predefined role's manifest
triggers Guard 1 — see [Governance: Guard 1](#governance-guard-1).

---

## Creating a Client Deployment

A client deployment override narrows a predefined role for one client — it
may subtract tools/permissions, never add them (see
[Two-Layer Deployment Model](#two-layer-deployment-model)). It lives in
**your own application's repository**, not in this one: this repo's
`deployments/` directory ships empty as the documented mount-point contract.

```bash
# In YOUR application's repo, not this one:
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

Point the platform at it explicitly — `RootConfig` never guesses a
consumer's own deployment tree:

```python
roots = agents_system.RootConfig(deployments_root=MY_APP_ROOT / "deployments")
equipped = agents_system.build_runtime(
    "my-analyst",
    registry,
    granted_permissions=["read:analytics", "read:reports"],
    client="acme-corp",
    roots=roots,
)
```

If `deployments_root` doesn't exist and a `client=` override is requested,
resolution raises `DefinitionError` instead of silently falling back to the
unrestricted predefined role.

Full reference: [`docs/platform/deployment.md`](docs/platform/deployment.md),
[`docs/platform/library-usage.md`](docs/platform/library-usage.md).

---

## Multi-Agent Delegation (Planned)

Two different things share the word "delegation" here:

- **This repository's own contribution workflow** — how multiple AI coding
  agents build `agents-system` itself — is tracked through GitHub Issues, not
  a shared ledger file. See [`AGENTS.md`](AGENTS.md).
- **Runtime delegation** — one agent role spawning a child agent role
  mid-conversation — is **declared but not implemented**. The `orchestrator`
  predefined role's manifest grants `spawn:sales-agent`, `spawn:data-agent`,
  and `spawn:summary-agent` permissions, and its `policy.md` declares a full
  `delegation_policy` (`allowed`, `permitted_child_roles`, `max_depth`) — but
  no `spawn` tool, connector, or runtime mechanism exists anywhere in
  `src/agents_system/`. `max_delegation_depth`/`max_clarification_attempts`
  are parsed but not yet enforced. This is tracked as future work, tentatively
  a future ADR.

Design notes for when it lands:
[`docs/architecture/delegation-policy.md`](docs/architecture/delegation-policy.md).
Current implementation status:
[`docs/delivery/live-test-plan.md`](docs/delivery/live-test-plan.md).

---

## Observability

- **`GET /health`** — liveness. Always returns HTTP 200; the body's `status`
  is `ok` or `degraded`. Reports PostgreSQL, Redis, webhook outbox state, chat
  provider reachability, and audit loss.
- **`GET /ready`** — readiness. Same body, but 200 only when `status` is
  `ok`; otherwise 503 (a core dependency is down, the provider is
  unreachable, or outbox work has expired/abandoned leases with no worker
  running).
- **`GET /metrics`** — Prometheus exposition, off by default
  (`METRICS_ENABLED=true`, bearer `METRICS_API_KEY`). Process, turn, tool,
  token, cost, and provider metrics: `agent_turns_total`,
  `agent_turn_duration_seconds`, `agent_tokens_total`, `agent_cost_usd_total`,
  `agent_tool_calls_total`, `agent_tool_call_duration_seconds`,
  `agent_limit_trips_total`, `agent_audit_events_dropped`,
  `agent_provider_requests_total`, `agent_provider_retries_total`, plus the
  standard `prometheus_client` process collector.

Full reference: [`docs/platform/observability.md`](docs/platform/observability.md).

---

## Production Deployment

Two supported paths, both documented end to end:

- **Docker Compose** — `docker-compose.prod.yml`: coordinated `postgres`,
  `redis`, `migrate`, and `app` services.
- **systemd** — `deploy/systemd/`: `agents-system-migrate.service`
  (`Type=oneshot`, runs migrations before anything else) and
  `agents-system-app.service` (`Type=simple`, the Uvicorn server with
  automatic restarts).

Both **migrate on deploy**: `alembic upgrade head` runs before the app
starts, and a non-zero exit aborts the deployment.

When any agent is registered (`AGENT_REGISTRATIONS` or
`create_app(agents=...)`), **`REGISTRY_FACTORY`** (a `module:callable`
returning a `ToolRegistry`) is required. The default factory only equips
generic stub tools and cannot equip role-specific tools (`catalog_search`,
`client_lookup`, `message_sender`, ...) — startup aborts immediately with a
`DefinitionError` naming what's missing.

Full reference: [`docs/operations/deployment.md`](docs/operations/deployment.md).

---

## Testing

The project uses **Strict TDD**: tests are written first (RED → GREEN).

```bash
# Offline suite (the default — live and load tests are excluded automatically)
uv run pytest

# Unit tests only (skip integration)
uv run pytest -m 'not integration'

# With coverage
uv run pytest --cov=agents_system

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/

# The built wheel, installed outside the repo, run as a library with an
# empty environment (see docs/operations/release-process.md)
scripts/smoke-installed-wheel.sh
```

Two opt-in suites run against real infrastructure and are never part of CI:

```bash
uv run --env-file .env pytest -m live               # live-eval pipeline against a real model (EVAL_PROVIDER in .env)
uv run --env-file .env pytest -m load  # concurrent-load scenarios against a real app
uv run --env-file .env pytest -m 'load and live'  # a small load run against a real model
```

They cover four phases: **Phase 1** (platform features end-to-end, HTTP API +
WhatsApp webhook), **Phase 2** — the guardrail suite — (Layer-2
revalidation, prompt-injection containment, sandbox containment, tool-call
and timeout limits; run with
`EVAL_RUNS=3 uv run --env-file .env pytest -m live tests/test_live_eval_guardrails.py -s`),
**Phase 3** (concurrent load through the webhook path), and **Phase 4** (a
full custom-agent lifecycle: define, register, serve, converse, audit,
update).

Full reference: [`docs/platform/live-eval.md`](docs/platform/live-eval.md).

The offline suite (30+ files) covers:
- Harness: loader, registry, injector, interceptor, factory
- Agent: graph, state, runtime nodes
- Integration: webhook, WhatsApp client, OpenAI adapter
- Services: Redis, embeddings, sync pipelines
- Connectors: RAG, SQL tool, sandboxed operator, stubs
- Models: SQLAlchemy ORM

---

## Governance: Guard 1

A predefined role's `tools`/`permissions` surface is a shared safety baseline
that other agents' `extends:` chains build on, so changing it is gated:

- A PR touching `platform/roles/**/manifest.md` must either match a reviewed,
  checked-in snapshot of the role's resolved `(tools, permissions)`
  (`tests/platform_role_contract.py`), or the role's `version:` frontmatter
  must take a MAJOR bump.
- The same PR must carry a conventional-commit breaking marker (`!` before
  the first `:` in the subject, or a `BREAKING CHANGE:` footer), checked by
  `scripts/check_role_governance_marker.py` — a `CHANGELOG.md` entry doesn't
  exist yet at PR time, so the marker is the enforceable equivalent.

Both `ci` and `predefined-role-governance-marker` are required GitHub status
checks on `main`. An importer-authored `Agent` that merely `extends:` a
predefined role is out of Guard 1's scope.

Full reference: [`docs/architecture/adr-004-library-first-agents.md`](docs/architecture/adr-004-library-first-agents.md).

---

## Docs

| Area | Path | Language |
|------|------|----------|
| Agent Platform Architecture | [`docs/architecture/`](docs/architecture/) | EN |
| Platform Specification | [`docs/platform/`](docs/platform/) | EN |
| Context-size control (model-input trimming, `max_context_tokens`) | [`docs/platform/context-size.md`](docs/platform/context-size.md), [`docs/platform_es/context-size.md`](docs/platform_es/context-size.md) | EN + ES |
| Conversation memory (durable Postgres checkpointer, retention, personal data) | [`docs/platform/conversation-memory.md`](docs/platform/conversation-memory.md), [`docs/platform_es/conversation-memory.md`](docs/platform_es/conversation-memory.md) | EN + ES |
| Feature reference (every feature, its entry point and its doc) | [`docs/platform/features.md`](docs/platform/features.md), [`docs/platform_es/features.md`](docs/platform_es/features.md) | EN + ES |
| Library Usage (integrating `agents_system` into your app) | [`docs/platform/library-usage.md`](docs/platform/library-usage.md), [`docs/platform_es/library-usage.md`](docs/platform_es/library-usage.md) | EN + ES |
| Especificación de Plataforma | [`docs/platform_es/`](docs/platform_es/) | ES |
| Production Operations (deployment, systemd, sandboxing) | [`docs/operations/`](docs/operations/) | EN + ES |
| Delivery Workflow | [`docs/delivery/`](docs/delivery/) | EN + ES |

---

## License

[MIT](LICENSE) © 2026 — Agents System Contributors.
