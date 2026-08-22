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

## Active wave: W4 — Runtime, deployment, generic agents (2026-08-17)

Full path, ordering rationale, and measured current state:
[`docs/delivery/w4-runtime-and-deployment.md`](docs/delivery/w4-runtime-and-deployment.md).

The order is dependency-driven: the topology decision (D-029) determines whether
guards can be in-process or must be Redis-backed, so it precedes them. The
webhook fix precedes the guards because it is what creates a place to admit
into — today the request *is* the work.

| ID | Slice / Feature | Agent | Cx | Branch | Depends on | Status | Result |
|----|-----------------|-------|----|--------|-----------|--------|--------|
| D-007-pr1 | Alembic infra + `AuditEvent` + table-ownership rule + partition lifecycle | claude-code | high | `feat/D-007-pr1-schema-model-migration` | — | in_review | 549 unit + 5 integration green, ruff + mypy clean. Fixed 4 defects beyond the original scope — see task details. 1282 lines, **size decision pending**. |
| D-026 | Audit events + redactor (Pydantic union, PII default-deny) | claude-code | high | `feat/D-007-pr2-events-redactor` | D-007-pr1 | in_review | 606 green, ruff + mypy clean. 1711 lines over pr1. Found the `client`/`deployment` mock divergence: every event recorded `"unknown"` as the deployment. Two strict xfails left as forcing functions for pr3. |
| D-027 | Audit sink wiring (injector, interceptor, factory, graph) | claude-code | high | `feat/D-007-pr3-sink-wiring` | D-026 | in_review | 633 green, ruff + mypy clean on 52 files. 1122 lines over pr2. Fixed 4 chained silent defects that made the whole feature inert — see task details. `AuditSink.stop()` tail loss pinned as strict xfail. |
| D-028 | Monthly partition job — operational entrypoint | — | low | — | D-007-pr1 | todo | Downgraded from critical to optimization by the DEFAULT partition. |
| D-041 | `AuditSink.stop()` loses the queue tail on graceful shutdown | — | medium | — | D-027 | todo | Pinned as `strict=True` xfail in `tests/test_audit_wiring.py`. `stop()` must await the drainer's exit instead of cancelling it. Remove the marker as part of the fix. |
| D-029 | Runtime topology decision record | — | medium | — | — | todo | Docs only. Blocks D-030/032/033/036. |
| D-030 | Webhook returns 200 before the turn; turn + send to background | — | high | — | D-029 | todo | Fixes silent message loss. |
| D-031 | Dedup claim/release so a failed turn does not swallow the message | — | medium | — | D-030 | todo | Must land with D-030 to be correct. |
| D-032 | Pool sizing from settings, both engines | — | low | — | D-029 | todo | Today: 15 connections total, defaulted. |
| D-033 | Admission control — bounded concurrent turns | — | medium | — | D-030 | todo | |
| D-034 | Per-client rate limiting | — | medium | — | D-033 | todo | |
| D-035 | Session invalidation on order confirmation | — | low | — | — | todo | Independent; can start any time. |
| D-036 | Server provisioning: units, migrations on deploy, health checks | — | high | — | D-029 | todo | |
| D-037 | Provider + model config per agent, no code edits | — | medium | — | — | todo | Manifests are already the surface. |
| D-038 | Configuration TUI over D-037 | — | medium | — | D-037 | todo | Last on purpose — ergonomics, not availability. |
| D-039 | Audit what the four platform roles actually do end to end | — | medium | — | — | todo | Only `sales-agent` is exercised. |
| D-040 | Multi-level and multi-role composition | — | high | — | D-039 | todo | Inheritance exists and can only subtract; the gaps are depth and the `loader.py:670` widening fallback. |

### Blocked / awaiting the human

| Item | Blocked on | Note |
|---|---|---|
| Real catalog for the sales agent | `MEDALLION_DB_*` in `.env` | `scripts/sync_articles.py` is finished and idempotent. Use `readonly_user`; `MEDALLION_DB_NAME=medallion_db` — the code default `medallion` is wrong. Until then the sales agent invents products. |
| Attack entry vector | Router admin UI at `192.168.1.1` | Host has no public IP and UPnP shows no mappings, so it is either a static forward/DMZ rule or a LAN-internal source. Settle it **before** a real BADIE credential lands here. |
| D-007-pr1 size | Your call | 1282 lines. ~240 is `alembic init` scaffolding (`alembic.ini`, `script.py.mako`, `uv.lock`) and 151 is the migration — both categories the `chained-pr` rule names as hard to split. Chain it (infra+model+ownership ≈700 / migration+partitions+CI ≈580, neither under 400) or record `size:exception`. |

### Known defects, filed not fixed

| Defect | Where | Why it matters |
|---|---|---|
| A typo'd deployment name silently **widens** the tool surface | `loader.py:670` — `resolve` falls through to the generic role when `load_override` returns `None` | Deployments may only narrow the platform role, so falling back to generic grants the role's **full** allowance instead of the deployment's subset. No log, no error. Security-relevant, not cosmetic. D-040 depends on this being closed. |
| Foreign-language token leakage | model output | The model has emitted Chinese and Russian into business-facing answers. Nothing detects it automatically. |
| Webhook answers Meta only after the full agent turn | `integration/webhook.py` | The turn's budget is 60s (`loader.py:114`) and the 200 comes after it plus the outbound send. Meta retries (`dedup.py:10` says so), the retry hits the dedup mark set *before* the turn, and the customer's message is dropped. Scheduled as D-030/D-031. |
| No concurrency guard of any kind | `src/` | `rg -i 'semaphore\|rate_limit\|limiter\|throttl\|max_concurren' src/` returns nothing. 1000 concurrent messages launch 1000 concurrent turns against the LLM quota. Scheduled as D-033/D-034. |

---

## Closed wave: W3 — Platform reuse + BI (2026-07-28 → 2026-08-17)

All ten PRs merged. `main` at `67592cb`: 536 unit tests, ruff and mypy clean.

| ID | Slice / Feature | Agent | Status |
|----|-----------------|-------|--------|
| D-023 | BI agent — parameterized report catalog over a read-only DB | claude-code | done (split into a 6-PR chain, #20–#25 + tracker #15) |
| D-024 | Public library API — client-injected ToolRegistry + shipped platform roles | claude-code + sonnet worker | done (PR #17) |
| D-025 | Dev-environment security record + `.gitignore` hygiene | claude-code | done (PR #16) |

Two defects were found only by merging all ten PRs into a scratch branch, which
per-PR CI cannot do — it builds each PR against its own base:

1. `run_report` made `data-agent` unbuildable outside `main.py`'s lifespan, and
   `data-agent` was **already** unbuildable on `main` (`knowledge_retrieval`
   with no connector). A manifest naming a tool the registry lacks raises
   `InjectionError` for the whole role, not part of it.
2. `agentsys.__dir__` leaked `Any`/`annotations`/`importlib` — found by building
   the wheel and installing it in a clean venv outside the repo.

Both closed. `data-agent` now resolves all five tools with zero denials.

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

### D-007-pr1 — Alembic infra, `AuditEvent`, table ownership, partition lifecycle

Branch `feat/D-007-pr1-schema-model-migration`, rebased on `67592cb`.
Gate: 549 unit + 5 integration passing, ruff clean, mypy clean on 47 files.

The slice arrived with the alembic scaffolding, the ORM model and its unit tests
already written. It was not mergeable: it left the suite at 2 failures and 18
errors, and three further defects only showed up under a real PostgreSQL. All
four are fixed, in four separate commits.

**1. Non-deterministic table inventory.** `audit_event` was reachable only by
importing `agentsys.models.audit_event` directly, so `Base.metadata` held five
tables or six depending on import order. `test_models` asserted the inventory
and so passed alone and failed in a full run; every SQLite fixture calling
`Base.metadata.create_all` raised `CompileError: SQLite does not support
autoincrement for composite primary keys` once the model was registered — 18
errors in modules unrelated to auditing. `agentsys/models/__init__.py` now
imports every model.

**2. ORM creating a table it cannot express.** `audit_event` is RANGE
partitioned; SQLAlchemy has no construct for that. Creating it from metadata
does not raise on PostgreSQL — it silently emits a plain unpartitioned table
that differs from what the migration builds. `scripts/init_db.py` did exactly
that. A table now declares `{"info": {ALEMBIC_OWNED: True}}` and
`create_orm_owned_tables` (in `models/base.py`) skips it; discovery is by the
flag, not a name list, so a second partitioned table inherits the behavior.
Both the fixtures and `init_db.py` go through it. Verified: after
`init_db.py` + `alembic upgrade head`, `audit_event` is `relkind='p'` and the
five ORM tables are `'r'`.

**3. The audit log expired on the calendar.** The migration created three
monthly partitions and no DEFAULT partition. PostgreSQL rejects a row outside
every partition (`no partition of relation "audit_event" found for row`), so
auditing would have stopped on the first day of the fourth month after install
— no code change, no deploy. The monthly partition job was therefore a hard
availability dependency. A DEFAULT partition removes that: verified against
PostgreSQL 16 that `occurred_at = now() + INTERVAL '10 years'` lands in
`audit_event_default` where it was previously rejected. Partition names are now
uniformly `audit_event_YYYY_MM` with explicit UTC bounds (they were
`_current`/`_next`/`_future`, a second convention that also stopped being true
after a month), and `downgrade` drops the parent in one statement instead of
three partitions by name — which orphaned anything added later.

**4. Migrations were not deployable.** `alembic.ini` carried
`sqlalchemy.url = postgresql+asyncpg://postgres:postgres@localhost:5432/badie`
and `env.py` read it from there, ignoring `DATABASE_URL` and
`Settings.database_url` both. Migrating a server meant editing a versioned file,
and a checked-in URL is how production credentials reach git history. `env.py`
now resolves `DATABASE_URL` → `Settings.database_url`. Also deleted a dead copy
of `create_audit_event_partition` from `env.py` that called `op.execute` with no
`op` in scope — a guaranteed `NameError`, and `op` only exists inside a
migration, so the "for cron use" it documented could never have worked.

New `audit-migration` CI job runs the migration tests against real PostgreSQL,
and the `bi-readonly` job's TODO is closed — it now runs `init_db.py` then
`alembic upgrade head`, which is the path production takes. These are
integration tests because they must be: the ORM model has no partitions, so no
unit test can tell whether a row lands in one, and SQLite cannot compile the
DDL. Wired into CI rather than left behind `-m integration`, because this
project already shipped a security check that sat behind that marker and ran
nowhere.

**Out of scope, deliberately:** the monthly partition job (D-028 — no longer
critical), and D-026/D-027, which remain `wip` and whose commits say not to open
a PR from them as-is.

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
