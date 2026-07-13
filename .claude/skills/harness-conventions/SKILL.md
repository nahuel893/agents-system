---
name: harness-conventions
description: "Trigger: adding a tool/connector, a role/deployment, or touching the agent runtime, interceptor, or RBAC in agents-system. Load before reading or writing code under src/agentsys/{harness,agent,connectors,integration}."
license: Apache-2.0
metadata:
  author: agents-system
  version: "1.0"
---

## What this is

`agents-system` is a reusable AI agent platform: a **generic** harness plus per-client
**injection** (a deployment supplies its own skills, tools, warehouse, and permissions).
Never hardcode client specifics into the platform — inject them from `deployments/<client>/`.

## Layered architecture (data flows top → bottom)

| Layer | Path | Responsibility |
|-------|------|----------------|
| Integration | `src/agentsys/integration/` | Entry points: `webhook.py` (WhatsApp), `openai_adapter.py` (Open WebUI). Resolve a cached runtime and call `run_turn`. Never build runtimes per request. |
| Harness | `src/agentsys/harness/` | `loader → injector → factory → interceptor` + `registry`. Assembles an `EquippedRuntime` and enforces RBAC. |
| Agent | `src/agentsys/agent/` | `graph.py` (LangGraph), `state.py`. `AgentRuntime.run_turn(...)`. |
| Connectors | `src/agentsys/connectors/` | Tools behind the interceptor (`rag_connector.py` = `catalog_search`). |
| Services | `src/agentsys/services/` | Domain logic + I/O (rag, catalog, orders, embeddings, redis, medallion). |
| Models | `src/agentsys/models/` | `base.py` (async engine), `tables.py` (ORM incl. `ConversationLog`). |

Cross-cutting: `config.py` (pydantic Settings — **`.env` overrides code defaults**), `observability/`.

## Tool / connector contract (D-009)

- Async-native: `async def connector(inputs, *, session) -> dict`. Sync connectors are offloaded via `asyncio.to_thread`.
- Register with a `ToolSpec(name, description, required_permissions, input_schema, connector)` on a `ToolRegistry` (`harness/registry.py`).
- A connector is generic: it receives its catalog/engine by **injection**, it does not embed a client's SQL or data.

## Security invariants (do not weaken)

- **Two RBAC layers.** Layer-1: only granted tools are equipped at build time. Layer-2 (`harness/interceptor.py`): sensitive tools are **revalidated at call time** against current permissions. Sensitive = `required_permissions` starts with `write:` / `send:`, **or** `ToolSpec.always_revalidate=True` (opt-in for sensitive reads).
- **Permissions are data-driven.** Grants come from the role/deployment definition the loader parses (`definition.permissions`) — never a hardcoded role→list map in `main.py`.
- **Execution limits are enforced, not just parsed.** `graph.py` bounds the loop by `execution_limits` (max_tool_calls → terminal node; per-call + per-turn `asyncio.timeout`). Defaults live in `loader.PLATFORM_DEFAULT_LIMITS` (20 / 60s / 10s).

## Runtime lifecycle & persistence

- Runtimes are built **once at startup** in `main.py:lifespan` and cached in `app.state.runtimes` keyed by `"{deployment}__{role}"`. Resolve, don't rebuild.
- Conversation persistence is **opt-in** per invocation: pass `thread_id` (WhatsApp = normalized phone) to engage the Redis checkpointer; omit it for stateless callers (Open WebUI sends full history). The checkpointer builds its **own** Redis connection (the shared pool uses `decode_responses=True`, which corrupts binary checkpoints).
- The system prompt is injected at model-call time, **not** persisted in graph state.

## When you add a capability

1. Add the platform capability generically (role in `platform/roles/`, connector in `connectors/`).
2. Inject client specifics under `deployments/<client>/`.
3. Wire it into the lifespan runtime cache + `adapter_runtimes` in config.
4. Strict TDD: write the failing test first (`uv run pytest`). See the `run-app` skill to exercise it end to end.
