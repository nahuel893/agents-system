# Library Usage

`agents_system` is not only the ACME runtime — it is an importable library. The
package ships the platform: the harness (loader → injector → factory) and
the GENERIC agent roles (`platform/roles/`). It does **not** ship any
client's tools or any client's deployment overrides. A consuming application
supplies both of those itself, at runtime, by injection — the user's own
words for the requirement this document exists to satisfy: *"el cliente debe
armar su propia tool registry e inyectarlo al agente."*

This document is for someone integrating `agents_system` into their own
application, not for someone working on the platform itself. For the
platform's own architecture, read `docs/platform/harness.md` and
`docs/platform/deployment.md` first.

---

## What the library gives you vs. what you bring

| Comes from `agents_system` | You bring it |
|---|---|
| The harness: `ToolRegistry`, `RootConfig`, `resolve`, `build_runtime`, `AgentRuntime` | Your own `ToolSpec`s — the connectors that call *your* services |
| The generic platform roles (`platform/roles/*/{role,manifest,policy}.md`) — what a `sales-agent`, `orchestrator`, `data-agent`, or `summary-agent` *is generically allowed to do* | Your own deployment overrides (`deployments/{your-client}/`), if you want client-specific prompts, skills, or tighter policy |
| RBAC enforcement (`InjectionError`, `FactoryError`, `DefinitionError`) | The permission grants for your caller/identity, and the `granted_permissions` you pass to `build_runtime` |
| A LangGraph-backed `AgentRuntime` that turns an equipped runtime into something you can call `run_turn` on | Any LangChain `BaseChatModel` to bind it to |

The platform never has business logic specific to any one client baked in —
including ACME, the platform's first deployment. If you find platform code
that assumes ACME, that is a bug, not a feature.

---

## Installation

From PyPI, once published:

```bash
uv add agents-system
# or
pip install agents-system
```

Until then, install directly from the git repository:

```bash
uv add "git+https://github.com/nahuel893/agents-system"
# or
pip install "git+https://github.com/nahuel893/agents-system"
```

Either way, the import name is `agents_system` (underscore) — only the
distribution name on the index is hyphenated.

Installing the package is enough to get the generic platform roles — they
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
| `resolve` | `agents_system.harness.loader` | Loads + merges a role definition (generic, optionally + a client override) |
| `DefinitionError` | `agents_system.harness.loader` | Raised when a definition or its roots are invalid |
| `build_runtime` | `agents_system.harness.factory` | The single choke point: role + your registry + your grants → `EquippedRuntime` |
| `EquippedRuntime` | `agents_system.harness.factory` | The fully assembled spec — tools, prompt, definition — `build_runtime` returns |
| `FactoryError` | `agents_system.harness.factory` | Raised when a runtime can't be assembled (e.g. a declared skill file is missing) |
| `InjectionError` | `agents_system.harness.injector` | Raised when a role declares a tool your registry doesn't have |
| `AgentRuntime` | `agents_system.agent.graph` | Wraps an `EquippedRuntime` + a chat model into something you call `run_turn` on |

`agents_system.__version__` is also available, resolved from the installed
package's metadata (falling back to a placeholder in a source checkout that
was never `pip install`-ed).

---

## Complete example

```python
import agents_system
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

registry = agents_system.ToolRegistry()
registry.register(
    agents_system.ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=my_catalog_search,
        tier=agents_system.Tier.T1,  # required (ADR-002 C.10) — T1: scoped read
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
# clone of `agents_system` you're running from — almost never what you want in
# a real application. If that default doesn't exist and you request a
# client override, `resolve()` now raises `DefinitionError` instead of
# silently falling back to the generic role. See "Trap 1" below.

roots = agents_system.RootConfig(
    deployments_root=MY_APP_ROOT / "deployments",
)

# --- 3. Build the equipped runtime --------------------------------------

equipped = agents_system.build_runtime(
    "sales-agent",
    registry,
    granted_permissions=["read:catalog", "send:message"],
    client="my-client",  # omit entirely to use the generic role, unmodified
    roots=roots,
)

# --- 4. Turn it into a live agent and run a turn ------------------------

model = ChatAnthropic(model="claude-sonnet-4-5")
agent = agents_system.AgentRuntime(equipped, model=model)

from langchain_core.messages import HumanMessage

reply = await agent.run_turn(
    [HumanMessage(content="What do you have in stock?")],
    session_id="session-123",
)
```

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
quietly falling back to the generic role. A deployment override can only
*narrow* the generic role, never broaden it — so a silent fallback would
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
generic roles.

### Trap 2 — `load_override` returns `None` silently on a typo'd client name

`agents_system.harness.loader.load_override` (`loader.py:290`) returns `None`
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
- Reference backends for the four platform-generic ports (knowledge, summarizer, escalation, order writer): `docs/platform/reference-backends.md`
- Source: `src/agents_system/harness/{registry,loader,injector,factory}.py`, `src/agents_system/agent/graph.py`, `src/agents_system/__init__.py`
