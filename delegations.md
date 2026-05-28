# Delegations — Multi-Agent Task Ledger

> **Lead/Planner:** Claude Code · **Transport:** Nahuel (human) · **Workers:** Claude Code, Antigravity, OpenCode
>
> Protocol: `docs/delivery/delegation-protocol.md`. Each agent works in an **isolated git worktree/branch**.
>
> **Find your work:** scan the wave table for the row(s) where the **Agent** column matches your identity (`claude-code`, `antigravity`, `opencode`). Those rows are yours — no one needs to hand you an ID.
>
> Then: read your row → do the task in your file scope → commit → save to Engram (`delegations/<id>`) → set status `in_review` → tell Nahuel.
>
> Status: `todo → in_progress → in_review → done` (or `blocked` with a note).

---

## Agent roster (edit to reassign roles / switch models)

| Slot | Agent | Model | Notes |
|------|-------|-------|-------|
| Lead / Integrator | claude-code | (session) | Plans, writes ledger, reviews, merges |
| Worker — complex | opencode | gpt-5.5 | High-complexity slices |
| Worker — mechanical | opencode | minimax-2.7 | Cheap / well-specified slices |
| Worker — multi-file/verify | antigravity | gemini-3.5-flash-high | Multi-file, browser/UI verification |

> To hand off a slot (token exhaustion, model switch): edit this table. The incoming agent reads Engram (`methodology/multi-agent-delegation` + `delegations/*`) and this ledger to recover context.

---

## Active wave: W1 — Harness build-out (2026-05-28)

| ID | Slice / Feature | Agent | Model | Cx | Branch | Depends on | Status | Result |
|----|-----------------|-------|-------|----|--------|-----------|--------|--------|
| D-002 | ToolRegistry + Capability Injector (Layer 1 enforcement) | opencode | gpt-5.4 | high | `feat/D-002-tool-registry-injector` | — | todo | — |

---

## Task details

### D-002 — ToolRegistry + Capability Injector (Layer 1 enforcement)

Build the second harness piece: the code-side `ToolRegistry` (the authority on what tools exist) and the capability injector that resolves the enforced tool surface for an agent. This is the security core — **the system provides and limits tools; the markdown only references names.**

> **⚠ Redo — isolation is mandatory.** A prior attempt was lost because work happened on the shared `main` checkout instead of an isolated worktree, and a `git reset` clobbered it. The worktree `../agents-system-D-002` is **already created** for you on branch `feat/D-002-tool-registry-injector` (from current `main`). `cd ../agents-system-D-002` and work ONLY there — never touch the main checkout, never `git reset` shared history. Your earlier tests survive as dangling commit `3f074e2` (e.g. `git show 3f074e2:tests/test_harness_registry.py`) if you want a head start, but redo with Strict TDD.

- **Agent:** opencode
- **Model:** gpt-5.4
- **Complexity:** high
- **Branch:** `feat/D-002-tool-registry-injector`
- **Worktree:** `../agents-system-D-002`
- **Depends on:** none (branch from current `main`, which already includes the D-001 loader hardening)
- **Wave:** W1
- **Strict TDD:** ACTIVE. Test runner: `pytest` (or `uv run pytest`). Write each failing test FIRST, watch it fail, then implement.
- **Skills to load:** none required (no matching project skill).
- **Read first:** `docs/platform/harness.md` (Capability Injector section), `docs/architecture/permission-model.md`, `docs/platform/tool.md`, and `src/badie/harness/loader.py` for the `AgentDefinition` shape. **Do NOT modify `loader.py`.**
- **Scope (files) — all NEW, disjoint from the loader:**
  - `src/badie/harness/registry.py`
  - `src/badie/harness/injector.py`
  - `tests/test_harness_registry.py`
  - `tests/test_harness_injector.py`
- **What to implement:**
  - **`registry.py`:**
    - `ToolSpec` (frozen dataclass): `name: str`, `required_permissions: tuple[str, ...]`, `connector: Callable[..., Any]` (the executable — real connectors come later; any callable is fine now).
    - `ToolNotFoundError(Exception)`.
    - `ToolRegistry` class: `register(spec)` (raise on duplicate name), `get(name) -> ToolSpec` (raise `ToolNotFoundError` if unknown), `__contains__(name)`, `names() -> tuple[str, ...]`.
  - **`injector.py`:**
    - `InjectionError(Exception)`.
    - `InjectionResult` (frozen dataclass): `granted: tuple[ToolSpec, ...]`, `denied: tuple[tuple[str, str], ...]` (each `(tool_name, reason)`).
    - `resolve_tool_surface(definition: AgentDefinition, registry: ToolRegistry, granted_permissions: Iterable[str]) -> InjectionResult`:
      1. `effective = set(definition.permissions) & set(granted_permissions)` — the role's permissions intersected with the requesting identity's RBAC grants.
      2. For each tool name in `definition.tools`:
         - if `name not in registry` → **raise `InjectionError`** (the manifest references a tool the system does not provide — fail loud, deterministic).
         - else `spec = registry.get(name)`; if `set(spec.required_permissions) <= effective` → add to `granted`; otherwise → add to `denied` with a reason naming the missing permissions.
      3. Return `InjectionResult(granted, denied)`.
- **Acceptance criteria:**
  - [ ] registry: `register` + `get` round-trip; `get(unknown)` raises `ToolNotFoundError`; duplicate `register` raises; `in` works; `names()` returns registered names.
  - [ ] resolve: all permissions present → every tool in `granted`, `denied` empty.
  - [ ] resolve: a tool whose `required_permissions` exceed `effective` → that tool in `denied` (reason names missing perms), the rest granted.
  - [ ] resolve: a tool allowed by the role's permissions but where the USER lacks the grant → `denied` (proves `effective = role ∩ granted`).
  - [ ] resolve: definition references a tool not in the registry → raises `InjectionError`.
  - [ ] Full suite green: `pytest`. `mypy src/` clean, `ruff check src/badie/harness/` clean.
- **Out of scope (do NOT build):** LangChain `bind_tools` / model binding; the Layer-2 runtime Tool Call Interceptor; real connector implementations; any change to `loader.py` or `test_harness_loader.py`.
- **Engram topic:** `delegations/D-002` — on finish, `mem_save` with `project: "agents-system"`, `topic_key: "delegations/D-002"`: what you built, the resolution algorithm, test results.
- **Do NOT:** merge to `main`, or touch any file outside the scope.
- **Status:** todo
- **Result:** _(fill when `in_review`: branch ready, test output summary, notes for the integrator)_

---

## Done (archive)

- **D-001** — Enforce `execution_limits` stricter-only invariant in loader (opencode/minimax-2.7). Merged to `main` (`00089c8`). Added `_PLATFORM_DEFAULT_LIMITS` + `_validate_execution_limits()` in `loader.py`; 19 loader tests, full suite 110 passed.
