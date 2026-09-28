# Library Usage

`agents_system` is not only the ACME runtime — it is an importable library. The
package ships the platform: the harness (loader → injector → factory) and
the PREDEFINED agent roles (`platform/roles/`). It does **not** ship any
client's tools or any client's deployment overrides. A consuming application
supplies both of those itself, at runtime, by injection — the user's own
words for the requirement this document exists to satisfy: *"el cliente debe
armar su propia tool registry e inyectarlo al agente."*

This document is for someone integrating `agents_system` into their own
application, not for someone working on the platform itself. For the
platform's own architecture, read `docs/platform/harness.md` and
`docs/platform/deployment.md` first.

---

## Quickstart

An agent with one tool of your own, running one turn. Nothing else is
registered: the escalation tool every agent inherits gets a built-in default
(see "Escalation: the built-in default" below).

<!-- runnable-example: quickstart -->
```python
import asyncio

from langchain_core.messages import HumanMessage

import agents_system


# Your tool: a function from a dict of inputs to a dict.
def word_count(inputs: dict, /) -> dict:
    return {"words": len(inputs["text"].split())}


registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="word_count",
        description="Count the words in a text.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        required_permissions=(),  # pure computation: nothing to grant
        connector=word_count,
        tier=agents_system.Tier.T0,
    )
)
agent = agents_system.Agent(name="word-counter", tools=("word_count",))
equipped = agents_system.build_runtime(agent, registry, granted_permissions=[])


async def main(model):
    runtime = agents_system.AgentRuntime(equipped, model=model)
    return await runtime.run_turn(
        [HumanMessage("How many words are in 'the quick brown fox'?")],
        session_id="quickstart",
    )


if __name__ == "__main__":
    from langchain_anthropic import ChatAnthropic  # reads ANTHROPIC_API_KEY

    messages = asyncio.run(main(ChatAnthropic(model="claude-sonnet-4-5")))
    print(messages[-1].content)
```

`tests/test_quickstart_example.py` executes this block as written, and
`scripts/smoke-installed-wheel.sh` runs it against the built wheel installed
outside the repository. Any LangChain chat model works in place of
`ChatAnthropic`.

- `Agent(...)` extends the basic `agent` role (conversation plus escalation)
  unless you pass `extends=`. Pass it straight to `build_runtime` or
  `resolve`.
- A tool that reads or changes your data declares a permission (for example
  `read:catalog`) and a tier to match, and the caller grants it in
  `granted_permissions`. The complete example below shows that.

---

## What the library gives you vs. what you bring

| Comes from `agents_system` | You bring it |
|---|---|
| The harness: `ToolRegistry`, `RootConfig`, `resolve`, `build_runtime`, `AgentRuntime` | Your own `ToolSpec`s — the connectors that call *your* services |
| The predefined platform roles (`platform/roles/*/{role,manifest,policy}.md`) — what a `sales-agent`, `orchestrator`, `data-agent`, or `summary-agent` *is allowed to do* | Your own deployment overrides (`deployments/{your-client}/`), if you want client-specific prompts, skills, or tighter policy |
| RBAC enforcement (`InjectionError`, `FactoryError`, `DefinitionError`) | The permission grants for your caller/identity, and the `granted_permissions` you pass to `build_runtime` |
| A LangGraph-backed `AgentRuntime` that turns an equipped runtime into something you can call `run_turn` on | Any LangChain `BaseChatModel` to bind it to |

The platform never has business logic specific to any one client baked in —
including ACME, the platform's first deployment. If you find platform code
that assumes ACME, that is a bug, not a feature.

---

## Use it from another repository

The package is not published to PyPI. Install it from a release tag with uv:

```bash
uv add "agents-system @ git+ssh://git@github.com/nahuel893/agents-system@vX.Y.Z"
```

This works while the repository is private, for anyone whose SSH key has
access to it; once it is public again,
`git+https://github.com/nahuel893/agents-system@vX.Y.Z` works without
credentials. Pin a tag rather than a branch, so every upgrade is a deliberate
change to your `pyproject.toml`; uv records the resolved commit in your
`uv.lock`. When no tag holds a change you need yet, pin its full commit hash
the same way (`...agents-system@<commit-sha>`) and move to the tag once it
is released.

A Docker build usually has no git credentials. Build the wheel where you do,
copy it into your application, and install the file:

```bash
# in a checkout of agents-system, at the tag you want
uv build --wheel   # writes dist/agents_system-X.Y.Z-py3-none-any.whl

# in your application
uv add ./wheels/agents_system-X.Y.Z-py3-none-any.whl
```

The image then needs the wheel in its build context before it installs
dependencies (for example `COPY wheels/ wheels/` ahead of `uv sync --frozen`).

The package depends on `torch` for local embeddings. This repository routes
it to the CPU-only PyTorch index in its own `[tool.uv.sources]`; add the same
`[tool.uv.sources]` and `[[tool.uv.index]]` entries to your application's
`pyproject.toml` if you want the same.

Either way, the import name is `agents_system` (underscore) — only the
distribution name on the index is hyphenated.

Installing the package is enough to get the predefined platform roles — they
ship inside the wheel (`platform/roles/**` is packaged as `agents_system/platform`
via `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`).
Your own deployment overrides are never part of the install; you keep them
in your own application's repository and point `RootConfig` at them.

---

## The public API

Everything below is reachable directly off the package (`import agents_system;
agents_system.X`), lazily — accessing a name imports only what that name needs,
nothing more:

| Name | Home module | What it is |
|---|---|---|
| `ToolRegistry` | `agents_system.harness.registry` | The registry you build and populate with your own tools |
| `ToolSpec` | `agents_system.harness.registry` | One tool's contract: name, required permissions, connector, input schema, capability `tier` |
| `Tier` | `agents_system.harness.registry` | Capability tier enum (`T0`-`T3`, ADR-002 C.10) — required on every `ToolSpec` |
| `ToolNotFoundError` | `agents_system.harness.registry` | Raised by `ToolRegistry.get()` for an unregistered name |
| `RootConfig` | `agents_system.harness.loader` | Injectable `platform_root` / `deployments_root` path pair |
| `AgentDefinition` | `agents_system.harness.loader` | The frozen, resolved role definition `resolve()` returns |
| `resolve` | `agents_system.harness.loader` | Loads + merges a role definition (predefined, optionally + a client override) |
| `DefinitionError` | `agents_system.harness.loader` | Raised when a definition or its roots are invalid |
| `build_runtime` | `agents_system.harness.factory` | The single choke point: role + your registry + your grants → `EquippedRuntime` |
| `EquippedRuntime` | `agents_system.harness.factory` | The fully assembled spec — tools, prompt, definition — `build_runtime` returns |
| `FactoryError` | `agents_system.harness.factory` | Raised when a runtime can't be assembled (e.g. a declared skill file is missing) |
| `InjectionError` | `agents_system.harness.injector` | Raised when a role declares a tool your registry doesn't have |
| `AgentRuntime` | `agents_system.agent.graph` | Wraps an `EquippedRuntime` + a chat model into something you call `run_turn` on |
| `Agent` | `agents_system.agent.spec` | A custom agent defined in Python (`Agent(...)`), from a folder (`Agent.from_folder(path)`), or both; pass it wherever a role name goes (`resolve`, `build_runtime`). See `docs/platform/role.md` |
| `ModelPrice` | `agents_system.config` | One model's price per million input/output tokens, for `AgentRuntime(model_prices=...)` |
| `AuditSink` | `agents_system.audit.sink` | The audit event sink; register one with `AuditSink.set_current(sink)` to record audit events (see "Running without the server") |

`agents_system.__version__` is also available, resolved from the installed
package's metadata (falling back to a placeholder in a source checkout that
was never `pip install`-ed).

---

## Complete example

This example runs exactly as written: `tests/test_readme_library_example.py`
executes it (with a scripted chat model passed to `main`), and
`scripts/smoke-installed-wheel.sh` runs the README's copy against the built
wheel, installed outside the repository, with an empty environment. It is
the same example as the README's.

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

Three things the example shows on purpose:

- **Every tool the role declares must be registered**, inherited ones
  included, with one exception: `escalation_notifier`, which every
  role-defined agent inherits from `platform/roles/agent`, gets a built-in
  default when your registry lacks it (see "Escalation: the built-in
  default"). `build_runtime` raises `InjectionError: Unknown tool: ...` for
  any other declared tool your registry lacks. Registering a tool is not
  granting it: a tool whose permission you do not pass in
  `granted_permissions` is denied at build time and never reaches the model,
  which is why a stub connector is enough for the tools this app does not use.
- **`granted_permissions` is the only grant.** Here the caller holds
  `read:catalog`, so `catalog_search` is the only tool equipped.
- **No deployment override.** Without `client=`, `build_runtime` uses the
  predefined role exactly as shipped, and `RootConfig` is not needed. To
  narrow the role for your own deployment, add both — see Trap 1 for why
  `deployments_root` must be explicit:

  ```python
  equipped = agents_system.build_runtime(
      "sales-agent",
      registry,
      granted_permissions=["read:catalog"],
      client="my-client",
      roots=agents_system.RootConfig(deployments_root=MY_APP_ROOT / "deployments"),
  )
  ```

---

## Running without the server

`build_runtime` → `AgentRuntime(...)` → `run_turn` never constructs the
server's `Settings` and needs none of its environment variables (no `.env`,
no `META_WEBHOOK_SECRET`, no database URL). Two server features are opt-in
instead of implicit:

### Cost: pass `model_prices`

The served app prices every turn from `MODEL_PRICES`. In library use the
runtime has no price table unless you give it one, keyed by the provider
model id (the same key `MODEL_PRICES` uses — see
`docs/platform/live-eval.md`):

```python
agent = agents_system.AgentRuntime(
    equipped,
    model=model,
    model_prices={
        "claude-sonnet-4-5": agents_system.ModelPrice(
            input_per_million=3.0, output_per_million=15.0
        ),
    },
)
result = await agent.run_turn_with_usage(messages, session_id="s1")
result.usage.cost_usd  # float, or None when the model id has no price
```

Without `model_prices`, `usage.cost_usd` is `None` — never a guessed number.
Token counts are reported either way.

### Audit: register an `AuditSink`

Audit events (tool granted/denied, tool call attempted/blocked, runtime
built, ...) go to the process's registered `AuditSink`. `create_app`'s
lifespan registers one on the application database. In library use nothing
is registered unless you register it, and the process logs a single
structured warning, `audit.sink_not_registered`, the first time an event has
nowhere to go — once per process, never a traceback per event.

To record them, construct the sink on an async SQLAlchemy session factory
whose database carries the platform's audit tables (created by this
repository's Alembic migrations — see `docs/platform/audit.md`), start it, and
register it for the whole process:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine(MY_DATABASE_URL)
sink = agents_system.AuditSink(
    session_factory=async_sessionmaker(engine, expire_on_commit=False)
)
await sink.start()
agents_system.AuditSink.set_current(sink)
try:
    ...  # build runtimes and run turns
finally:
    await sink.stop()  # drains what is still queued
    agents_system.AuditSink.clear_current(sink)
```

The registration is process-wide: every task records into the same sink,
whichever task registered it.

### Escalation: the built-in default

Every role-defined agent inherits `escalation_notifier` (`send:escalation`,
tier T2) from `platform/roles/agent`. When your registry has none,
`build_runtime` fills it with a built-in default so the agent still builds.
That default:

- **reaches no human.** It answers the model with the platform's "no
  escalation channel is configured" text, so the model tells the user nobody
  was reached instead of promising a callback;
- logs a WARNING, `escalation.not_delivered`, and records an
  `escalation_not_delivered` audit event, on every call. Both carry the role,
  the reason (cut to 200 characters and passed through your audit policy's
  redactor) and the length of the details. Neither stores the details text:
  it is free text about the customer, such as names, IDs or addresses, which
  the redactor has no pattern for;
- is equipped only when you grant `send:escalation`, and then logs a WARNING,
  `injector.builtin_default_granted`, at build time. The Quickstart grants
  nothing, so there it is denied and never reaches the model;
- is replaced by any `escalation_notifier` you register. The
  `EscalationChannel` port behind the real tool is described in
  `docs/platform/reference-backends.md`.

The served app never uses it. `create_app`'s lifespan builds with
`build_runtime(..., builtin_defaults=False)`, so a served role without its own
`escalation_notifier` fails boot with `InjectionError: Unknown tool:
escalation_notifier`, and a registry that holds the default (registered by
hand) is refused at boot. Pass `builtin_defaults=False` yourself for the same
fail-closed behaviour in-process.

---

## Two traps

### Trap 1 — `deployments_root` defaults to the dev checkout

If you construct `agents_system.RootConfig()` with no arguments (or omit
`deployments_root` from an explicit `RootConfig(...)` call), it defaults to
the `deployments/` directory of whatever git checkout of `agents_system` your
Python environment happens to be running from — i.e. **this** repository's
`deployments/acme/`, not yours. `platform_root` has a real installed-package
default (see below); `deployments_root` deliberately does not, because a
client's deployments are never shipped inside the package.

**As of D-024 slice 2, forgetting this no longer fails silently in the case
that matters most**: if you request a client override (`resolve(role,
client=...)` / `build_runtime(..., client=...)`) and the resolved
`deployments_root` directory does not exist at all (e.g. `agents_system` installed
as a dependency, with no co-located `deployments/`), the library raises an
explicit `DefinitionError` naming the exact path it looked for, instead of
quietly falling back to the predefined role. A deployment override can only
*narrow* the predefined role, never broaden it — so a silent fallback would
have widened tools/autonomy/permissions past what the requested (but
unconfigured) override was meant to restrict; that is a security relaxation
disguised as a safe default, which is why this is now a loud failure rather
than a warning log.

This does **not** make omitting `deployments_root` safe: the guessed default
still only matches a co-located dev checkout of `agents_system` itself. If your
application happens to run from a clone where that directory exists for an
unrelated reason, you will silently get ACME's overrides instead of an
error — the new guard only catches the *missing-directory* case, not
*exists-but-is-the-wrong-one*. **Always pass `deployments_root` explicitly**
when you have your own deployment overrides; do not rely on the guessed
default succeeding just because it no longer fails silently when absent.

`platform_root`, by contrast, resolves automatically: `RootConfig()` tries
the packaged location (`platform/` next to the installed `agents_system` package)
first, then the dev-checkout location, and raises `DefinitionError` naming
both attempted paths only if neither exists
(`src/agents_system/harness/loader.py`, `_default_platform_root`). You only need
to pass `platform_root` yourself if you're shipping your own fork of the
predefined roles.

### Trap 2 — `load_override` returns `None` silently on a typo'd client name

`agents_system.harness.loader.load_override` (`loader.py:290`) returns `None`
when `deployments_root/{client}/{role_type}/` does not exist on disk —
**silently, no warning, no error**:

```python
if not folder.exists():
    return None  # loader.py:303
```

`resolve(role_type, client=..., roots=...)` treats a `None` override exactly
like "this client has no override" and falls back to the predefined role
definition. That means a typo in `client=` (`"my-cilent"` instead of
`"my-client"`) does not raise — it quietly hands you back the predefined role
with none of your deployment's prompts, skills, or policy restrictions
applied. This behavior is unchanged by this document; it is documented here
so you know to check `AgentDefinition.deployment is not None` (or log
`definition.deployment`) if you need to confirm an override actually applied.

---

## Cross-references

- Harness pipeline and injection order: `docs/platform/harness.md`
- Predefined role vs. deployment override semantics: `docs/platform/deployment.md`
- Role/manifest/policy file schema: `docs/platform/role.md`, `docs/platform/policy.md`
- Tool contract (`ToolSpec`, connector signature): `docs/platform/tool.md`
- Audit sink, events and storage: `docs/platform/audit.md`
- Token usage and cost (`MODEL_PRICES`): `docs/platform/live-eval.md`
- Reference backends for the four platform-generic ports (knowledge, summarizer, escalation, order writer): `docs/platform/reference-backends.md`
- Source: `src/agents_system/harness/{registry,loader,injector,factory,defaults}.py`, `src/agents_system/agent/graph.py`, `src/agents_system/audit/sink.py`, `src/agents_system/__init__.py`
- Spanish version: `docs/platform_es/library-usage.md`
