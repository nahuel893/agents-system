# Library Usage

`agentsys` is not only the BADIE runtime — it is an importable library. The
package ships the platform: the harness (loader → injector → factory) and
the GENERIC agent roles (`platform/roles/`). It does **not** ship any
client's tools or any client's deployment overrides. A consuming application
supplies both of those itself, at runtime, by injection — the user's own
words for the requirement this document exists to satisfy: *"el cliente debe
armar su propia tool registry e inyectarlo al agente."*

This document is for someone integrating `agentsys` into their own
application, not for someone working on the platform itself. For the
platform's own architecture, read `docs/platform/harness.md` and
`docs/platform/deployment.md` first.

---

## What the library gives you vs. what you bring

| Comes from `agentsys` | You bring it |
|---|---|
| The harness: `ToolRegistry`, `RootConfig`, `resolve`, `build_runtime`, `AgentRuntime` | Your own `ToolSpec`s — the connectors that call *your* services |
| The generic platform roles (`platform/roles/*/{role,manifest,policy}.md`) — what a `sales-agent`, `orchestrator`, `data-agent`, or `summary-agent` *is generically allowed to do* | Your own deployment overrides (`deployments/{your-client}/`), if you want client-specific prompts, skills, or tighter policy |
| RBAC enforcement (`InjectionError`, `FactoryError`, `DefinitionError`) | The permission grants for your caller/identity, and the `granted_permissions` you pass to `build_runtime` |
| A LangGraph-backed `AgentRuntime` that turns an equipped runtime into something you can call `run_turn` on | Any LangChain `BaseChatModel` to bind it to |

The platform never has business logic specific to any one client baked in —
including BADIE, the platform's first deployment. If you find platform code
that assumes BADIE, that is a bug, not a feature.

---

## Installation

```bash
uv add agentsys
# or
pip install agentsys
```

Installing the package is enough to get the generic platform roles — they
ship inside the wheel (`platform/roles/**` is packaged as `agentsys/platform`
via `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`).
Your own deployment overrides are never part of the install; you keep them
in your own application's repository and point `RootConfig` at them.

---

## The public API

Everything below is reachable directly off the package (`import agentsys;
agentsys.X`), lazily — accessing a name imports only what that name needs,
nothing more:

| Name | Home module | What it is |
|---|---|---|
| `ToolRegistry` | `agentsys.harness.registry` | The registry you build and populate with your own tools |
| `ToolSpec` | `agentsys.harness.registry` | One tool's contract: name, required permissions, connector, input schema |
| `ToolNotFoundError` | `agentsys.harness.registry` | Raised by `ToolRegistry.get()` for an unregistered name |
| `RootConfig` | `agentsys.harness.loader` | Injectable `platform_root` / `deployments_root` path pair |
| `AgentDefinition` | `agentsys.harness.loader` | The frozen, resolved role definition `resolve()` returns |
| `resolve` | `agentsys.harness.loader` | Loads + merges a role definition (generic, optionally + a client override) |
| `DefinitionError` | `agentsys.harness.loader` | Raised when a definition or its roots are invalid |
| `build_runtime` | `agentsys.harness.factory` | The single choke point: role + your registry + your grants → `EquippedRuntime` |
| `EquippedRuntime` | `agentsys.harness.factory` | The fully assembled spec — tools, prompt, definition — `build_runtime` returns |
| `FactoryError` | `agentsys.harness.factory` | Raised when a runtime can't be assembled (e.g. a declared skill file is missing) |
| `InjectionError` | `agentsys.harness.injector` | Raised when a role declares a tool your registry doesn't have |
| `AgentRuntime` | `agentsys.agent.graph` | Wraps an `EquippedRuntime` + a chat model into something you call `run_turn` on |

`agentsys.__version__` is also available, resolved from the installed
package's metadata (falling back to a placeholder in a source checkout that
was never `pip install`-ed).

---

## Complete example

```python
import agentsys
from langchain_anthropic import ChatAnthropic

# --- 1. Build your own registry and your own tools ---------------------
#
# The connector is YOUR code. It can call your database, your APIs, your
# vector store — the platform doesn't care. The tool NAME has to match a
# name a platform role's manifest.md declares (see platform/roles/*/manifest.md)
# for the injector to be able to grant it.

def my_catalog_search(inputs: dict, /) -> dict:
    query = inputs["query"]
    return {"results": my_catalog_service.search(query)}

registry = agentsys.ToolRegistry()
registry.register(
    agentsys.ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=my_catalog_search,
        description="Search the product catalog by free-text query.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
)
# ... register every other tool the role you're building declares. The
# injector raises InjectionError for any role-declared tool your registry
# doesn't have — see "Trap 2" below.

# --- 2. Point RootConfig at YOUR deployment tree ------------------------
#
# Omit `deployments_root` and it defaults to the DEV CHECKOUT of whichever
# clone of `agentsys` you're running from — almost never what you want in
# a real application. See "Trap 1" below.

roots = agentsys.RootConfig(
    deployments_root=MY_APP_ROOT / "deployments",
)

# --- 3. Build the equipped runtime --------------------------------------

equipped = agentsys.build_runtime(
    "sales-agent",
    registry,
    granted_permissions=["read:catalog", "send:message"],
    client="my-client",  # omit entirely to use the generic role, unmodified
    roots=roots,
)

# --- 4. Turn it into a live agent and run a turn ------------------------

model = ChatAnthropic(model="claude-sonnet-4-5")
agent = agentsys.AgentRuntime(equipped, model=model)

from langchain_core.messages import HumanMessage

reply = await agent.run_turn(
    [HumanMessage(content="What do you have in stock?")],
    session_id="session-123",
)
```

---

## Two traps

### Trap 1 — `deployments_root` defaults to the dev checkout

If you construct `agentsys.RootConfig()` with no arguments (or omit
`deployments_root` from an explicit `RootConfig(...)` call), it defaults to
the `deployments/` directory of whatever git checkout of `agentsys` your
Python environment happens to be running from — i.e. **this** repository's
`deployments/badie/`, not yours. `platform_root` has a real installed-package
default (see below); `deployments_root` deliberately does not, because a
client's deployments are never shipped inside the package. Forgetting to
pass your own `deployments_root` silently means you either get BADIE's
overrides (if this repo happens to be on the path) or a plain
`FileNotFoundError`-shaped surprise. **Always pass `deployments_root`
explicitly** when you have your own deployment overrides.

`platform_root`, by contrast, resolves automatically: `RootConfig()` tries
the packaged location (`platform/` next to the installed `agentsys` package)
first, then the dev-checkout location, and raises `DefinitionError` naming
both attempted paths only if neither exists
(`src/agentsys/harness/loader.py`, `_default_platform_root`). You only need
to pass `platform_root` yourself if you're shipping your own fork of the
generic roles.

### Trap 2 — `load_override` returns `None` silently on a typo'd client name

`agentsys.harness.loader.load_override` (`loader.py:290`) returns `None`
when `deployments_root/{client}/{role_type}/` does not exist on disk —
**silently, no warning, no error**:

```python
if not folder.exists():
    return None  # loader.py:303
```

`resolve(role_type, client=..., roots=...)` treats a `None` override exactly
like "this client has no override" and falls back to the generic role
definition. That means a typo in `client=` (`"my-cilent"` instead of
`"my-client"`) does not raise — it quietly hands you back the generic role
with none of your deployment's prompts, skills, or policy restrictions
applied. This behavior is unchanged by this document; it is documented here
so you know to check `AgentDefinition.deployment is not None` (or log
`definition.deployment`) if you need to confirm an override actually applied.

---

## Cross-references

- Harness pipeline and injection order: `docs/platform/harness.md`
- Generic role vs. deployment override semantics: `docs/platform/deployment.md`
- Role/manifest/policy file schema: `docs/platform/role.md`, `docs/platform/policy.md`
- Tool contract (`ToolSpec`, connector signature): `docs/platform/tool.md`
- Source: `src/agentsys/harness/{registry,loader,injector,factory}.py`, `src/agentsys/agent/graph.py`, `src/agentsys/__init__.py`
