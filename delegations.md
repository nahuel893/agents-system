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
| D-003 | Sync architecture diagram + complete Spanish docs | antigravity | gemini-3.5-flash-high | low | `feat/D-003-docs-diagram-sync` | — | in_review | Committed on branch (`5581b57`); pending Lead review |

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
- **Status:** in_progress
- **Result:** _(fill when `in_review`: branch ready, test output summary, notes for the integrator)_

---

### D-003 — Sync architecture diagram + complete Spanish docs

Bring the interactive diagram and the Spanish docs in line with the **current `main`** architecture. Two deliverables, both disjoint from D-002's code.

- **Agent:** antigravity
- **Model:** gemini-3.5-flash-high
- **Complexity:** low (well-specified — no design decisions)
- **Branch:** `feat/D-003-docs-diagram-sync`
- **Worktree:** `../agents-system-D-003` — **already created for you.** `cd ../agents-system-D-003` and work ONLY there. Never touch the main checkout, never `git reset`/`checkout` shared history.
- **Depends on:** none (syncs current `main`)
- **Wave:** W1
- **Read first:** `docs/platform/deployment.md`, `docs/platform/role.md`, `docs/platform/policy.md`, the English `docs/platform/*.md`, and `docs/architecture/diagram.html`.
- **Scope (files) — disjoint from D-002:**
  - `docs/architecture/diagram.html`
  - `docs/platform_es/*.md` (update existing; add new `policy.md` + `deployment.md`)
- **Part A — `diagram.html`:**
  - **View 3 (Agent Definition Structure)** currently shows a single `agents/preventa/` folder. Replace it with the **two-layer deployment model** from `deployment.md`: `platform/roles/{role}/` (generic: `role.md` + `manifest.md` + `policy.md`) and `deployments/{client}/{role}/` (override: same three + `skills/`).
  - Note that `manifest.md`/`policy.md` are machine-readable **YAML frontmatter** and `role.md` prose is the system prompt.
  - Show the inheritance rule (override ⊆ parent: tools subset, `permissions: inherit`, autonomy ceiling).
  - Verify Views 1 (pipeline) and 2 (agent model) are still accurate; keep the enforcement-layers panel.
  - Must stay **self-contained** (no new external/CDN deps) and open in a browser without errors.
- **Part B — `docs/platform_es/`:**
  - Add `docs/platform_es/policy.md` and `docs/platform_es/deployment.md`, translated from the English `docs/platform/policy.md` and `deployment.md`.
  - Update the existing `docs/platform_es/{harness,manifesto,role,skill,tool}.md` from single-file "manifiesto" wording to the **folder-based agent-definition** terminology, matching the English source of truth.
- **Acceptance criteria:**
  - [ ] `diagram.html` View 3 shows the two-layer `platform/roles` + `deployments` model and notes the YAML-frontmatter format; opens in a browser, still self-contained.
  - [ ] `docs/platform_es/` has all 7 files, consistent with the English versions and the folder-based model.
  - [ ] No code touched; no English `docs/platform/` source modified (Spanish + diagram only).
- **Out of scope (do NOT do):** document the `ToolRegistry`/injector (D-002 is unmerged — only document what's on `main`); touch any code; edit `delegations.md`.
- **Engram topic:** `delegations/D-003` — on finish, `mem_save` with `project: "agents-system"`, `topic_key: "delegations/D-003"`.
- **Reporting:** the Lead owns `delegations.md` — do **not** edit it. Report status by telling Nahuel + saving to Engram.
- **Status:** todo
- **Result:** _(fill when `in_review`)_

---

## Done (archive)

- **D-001** — Enforce `execution_limits` stricter-only invariant in loader (opencode/minimax-2.7). Merged to `main` (`00089c8`). Added `_PLATFORM_DEFAULT_LIMITS` + `_validate_execution_limits()` in `loader.py`; 19 loader tests, full suite 110 passed.
- **D-002** — ToolRegistry + capability injector / Layer 1 enforcement (opencode/gpt-5.4). Merged to `main` (`a82101a`). `registry.py` (tool authority) + `injector.py` (`resolve_tool_surface`, effective = role ∩ granted, fail-loud on unknown tool); 8 tests, full suite 118 passed. Note: worker did not commit — Lead committed the work at integration.
