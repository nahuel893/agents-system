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

> **Where status lives.** This ledger is the authority on **agent assignment and isolation** — who owns which slice, on which branch, in which worktree. **Notion is the authority on project/task status** (Proyectos → D-0XX, with linked Tareas). When the two disagree, Notion wins for *status* and this file wins for *ownership*. Technical prose belongs in `docs/` and Engram, never here.
>
> **This file drifted.** Wave W1 stayed marked "active" from 2026-05-28 until 2026-07-29 while work ran through D-024, so anyone following rule 1 above read a two-month-old picture. If you close a wave, say so in the same commit that closes it.

## Active wave: W3 — Platform reuse + BI (2026-07-28)

| ID | Slice / Feature | Agent | Model | Cx | Branch | Depends on | Status | Result |
|----|-----------------|-------|-------|----|--------|-----------|--------|--------|
| D-023 | BI agent — parameterized report catalog over a read-only DB | claude-code | (session) | high | `feat/D-023-bi-agent` | — | in_review | PR #15, CI green. 2615 lines — **size decision pending**: chain or `size:exception`. Every reported figure reconciles with hand-run SQL. |
| D-024 | Public library API — client-injected ToolRegistry + shipped platform roles | claude-code + sonnet worker | sonnet | medium | `feat/D-024-library-api` | — | in_review | PR #17. Verified against an installed wheel run from outside the repo: `platform_root` → `site-packages/agentsys/platform`, `resolve('sales-agent')` OK, no FastAPI/LangGraph at import. |
| D-025 | Dev-environment security record + `.gitignore` hygiene | claude-code | (session) | low | `docs/dev-environment-security` | — | in_review | PR #16, CI green. Docs + config only. |

### Blocked / awaiting the human

| Item | Blocked on | Note |
|---|---|---|
| Real catalog for the sales agent | `MEDALLION_DB_*` in `.env` | `scripts/sync_articles.py` is finished and idempotent. Use `readonly_user`; `MEDALLION_DB_NAME=medallion_db` — the code default `medallion` is wrong. Until then the sales agent invents products. |
| Attack entry vector | Router admin UI at `192.168.1.1` | Host has no public IP and UPnP shows no mappings, so it is either a static forward/DMZ rule or a LAN-internal source. Settle it **before** a real BADIE credential lands here. |
| PR #15 size | Your call | Chain it or record `size:exception`. |

### Known defects, filed not fixed

| Defect | Where | Why it matters |
|---|---|---|
| A typo'd deployment name silently **widens** the tool surface | `loader.py:670` — `resolve` falls through to the generic role when `load_override` returns `None` | Deployments may only narrow the platform role, so falling back to generic grants the role's **full** allowance instead of the deployment's subset. No log, no error. Security-relevant, not cosmetic. |
| Foreign-language token leakage | model output | The model has emitted Chinese and Russian into business-facing answers. Nothing detects it automatically. |

---

## Closed wave: W1 — Harness build-out (2026-05-28 → 2026-06)

All slices merged to `main`; details in the archive at the bottom of this file.

| ID | Slice / Feature | Agent | Status |
|----|-----------------|-------|--------|
| D-003 | Sync architecture diagram + complete Spanish docs | antigravity | done (`351be60`) |
| D-004 | Agent Factory — assemble EquippedRuntime | claude-code | done |
| D-005 | Tool Call Interceptor — Layer-2 enforcement | claude-code | done |
| D-006 | BADIE connector stubs — 5 sales-agent tools | claude-code | done — `src/agentsys/connectors/stubs.py`, wired from `main.py` |

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

### D-004 — Agent Factory (EquippedRuntime assembler)

The keystone glue: assembles `loader.resolve()` + `injector.resolve_tool_surface()` + loaded skill files into a single ready-to-run `EquippedRuntime`. Built interactively with the Lead. Serves the **agent_seller MVP** — `badie/sales-agent` now assembles end-to-end.

- **Agent:** claude-code (Lead, interactive)
- **Model:** (session)
- **Complexity:** high
- **Branch:** `feat/D-004-agent-factory`
- **Depends on:** D-002 (registry + injector, on `main`)
- **Wave:** W1
- **Strict TDD:** ACTIVE — tests written first (RED → GREEN).
- **Scope (files) — all NEW:**
  - `src/agentsys/harness/factory.py`
  - `tests/test_harness_factory.py`
- **What was built:**
  - `build_runtime(role_type, registry, granted_permissions, *, client=None, roots=None) -> EquippedRuntime`.
  - `EquippedRuntime` (frozen): `definition`, `system_prompt` (composed), `tools` (granted `ToolSpec`s), `denied_tools` (`tuple[name, reason]`), `skills` (`tuple[LoadedSkill]`).
  - `LoadedSkill(name, content)`, `FactoryError`.
  - Prompt composition = role body + each skill file verbatim, joined by `---`, in manifest order. Skills load from `deployments/{client}/{role_type}/skills/{name}.md`; missing file → `FactoryError`. Structured events: `factory.skill_loaded`, `factory.skill_missing`, `factory.runtime_built`.
- **Acceptance criteria:**
  - [x] Resolves definition + grants/denies tools via injector.
  - [x] Loads declared skills in order; missing skill file → `FactoryError`.
  - [x] Composes prompt (role body before skills); generic role (no client) → role body alone, no skills.
  - [x] Full suite green (166 passed), factory 100% cov, ruff clean, mypy clean on factory files.
- **Out of scope (later slices):** LangChain `bind_tools` / model binding / LLM call (D-007 Agent Runtime); Layer-2 Tool Call Interceptor (D-005).
- **Engram topic:** `delegations/D-004`.
- **SDD:** Artifacts backfilled to engram (`sdd/D-004/{spec,design,tasks,apply-progress}`). Verify run by a fresh `sdd-verify` sub-agent (sonnet) → **PASS** (`sdd/D-004/verify-report`, obs #210): 0 CRITICAL, 1 WARNING (R5 events emitted but not asserted) + 1 SUGGESTION. WARNING-1 closed by adding explicit assertions for `factory.skill_loaded`/`factory.skill_missing` (`4f118b9`). SUGGESTION-1 (generic test uses real roots) left as intentional design choice — happy path deliberately exercises the real BADIE MVP deployment.
- **Status:** in_review
- **Result:** Branch `feat/D-004-agent-factory` (commits `0c72cca`, `adc8335`, `b027b65`, `4f118b9`). 11 factory tests, 100% factory coverage, full suite 168 passed, ruff + mypy clean on factory. Verify PASS. Pending merge to `main` by Lead at integration.

---

### D-005 — Tool Call Interceptor (Layer-2 execution-time enforcement)

The second enforcement layer: validates every tool call at execution time against the `EquippedRuntime`'s injected surface, blocks anything not in the surface, and revalidates permissions for sensitive tools (write:\* / send:\*) before the connector fires.

- **Agent:** claude-code (Lead, interactive)
- **Model:** (session)
- **Complexity:** medium
- **Branch:** `feat/D-005-tool-call-interceptor`
- **Depends on:** D-004 (`EquippedRuntime` shape)
- **Wave:** W1
- **Strict TDD:** ACTIVE — RED → GREEN, one test at a time.
- **Scope (files) — all NEW:**
  - `src/agentsys/harness/interceptor.py`
  - `tests/test_harness_interceptor.py`
- **What to implement:**
  - **`PolicyViolation(Exception)`** — raised on any blocked call. Carries `tool_name`, `reason`.
  - **`CallResult`** (frozen dataclass): `tool_name: str`, `output: Any`, `revalidated: bool`.
  - **`intercept(tool_name, tool_input, runtime, *, current_permissions=None) -> CallResult`**:
    1. Look up tool by name in `runtime.tools` (the granted `ToolSpec` tuple).
    2. If NOT found → log `interceptor.call_blocked` (reason: `not_in_surface`) → raise `PolicyViolation`.
    3. If found: check if **sensitive** — a tool is sensitive if any `required_permissions` starts with `"write:"` or `"send:"`.
    4. If sensitive and `current_permissions is None` → log `interceptor.call_blocked` (reason: `revalidation_required`) → raise `PolicyViolation`.
    5. If sensitive and `current_permissions` given → recheck `set(spec.required_permissions) <= set(current_permissions)`; if fails → log `interceptor.call_blocked` (reason: `permission_revoked`) → raise `PolicyViolation`.
    6. Log `interceptor.call_allowed`. Execute `spec.connector(tool_input)`. Log `interceptor.call_executed`. Return `CallResult(tool_name, output, revalidated=is_sensitive)`.
- **Acceptance criteria:**
  - [ ] Non-sensitive tool in surface, no `current_permissions` → executes, `revalidated=False`.
  - [ ] Tool NOT in surface → raises `PolicyViolation`, logs `interceptor.call_blocked`.
  - [ ] Sensitive tool, sufficient `current_permissions` → executes, `revalidated=True`.
  - [ ] Sensitive tool, insufficient `current_permissions` → raises `PolicyViolation`, logs `interceptor.call_blocked`.
  - [ ] Sensitive tool, `current_permissions=None` → raises `PolicyViolation` (can't revalidate).
  - [ ] Non-sensitive tool, `current_permissions` irrelevant (not revalidated even if provided).
  - [ ] All three events (`call_blocked`, `call_allowed`, `call_executed`) emitted at the right moments.
  - [ ] Full suite green (`uv run pytest`), `mypy` + `ruff` clean on new files.
- **Out of scope:** delegation interceptor, audit persistence, escalation signalling (those are D-007+), connector timeout enforcement.
- **Status:** in_review
- **Result:** `interceptor.py` + `test_harness_interceptor.py`. 9 tests (all criteria covered), 177 full suite passed, ruff + mypy clean. Pending merge to `main`.

---

## Done (archive)

- **D-001** — Enforce `execution_limits` stricter-only invariant in loader (opencode/minimax-2.7). Merged to `main` (`00089c8`). Added `_PLATFORM_DEFAULT_LIMITS` + `_validate_execution_limits()` in `loader.py`; 19 loader tests, full suite 110 passed.
- **D-002** — ToolRegistry + capability injector / Layer 1 enforcement (opencode/gpt-5.4). Merged to `main` (`a82101a`). `registry.py` (tool authority) + `injector.py` (`resolve_tool_surface`, effective = role ∩ granted, fail-loud on unknown tool); 8 tests, full suite 118 passed. Note: worker did not commit — Lead committed the work at integration.
- **D-003** — Sync architecture diagram + complete Spanish docs (antigravity/gemini-3.5-flash-high). Merged to `main` (`351be60`). `diagram.html` View 3 updated to two-layer model; `docs/platform_es/` complete (7 files); extras: `docs/architecture_es/`, `docs/delivery_es/`, Outline scripts.
- **D-004** — Agent Factory / EquippedRuntime assembler (claude-code). Merged to `main`. `factory.py`: `build_runtime()` glues loader + injector + skill files → frozen `EquippedRuntime`. Prompt = role body + skills joined by `---`. 11 tests, 100% factory coverage, verify PASS (SDD sub-agent sonnet).
- **D-005** — Tool Call Interceptor / Layer-2 enforcement (claude-code). Merged to `main`. `interceptor.py`: `intercept()` validates call against surface, revalidates sensitive tools at call time. `PolicyViolation` + `CallResult`. 9 tests, 177 suite, ruff+mypy clean. Verify PASS.
