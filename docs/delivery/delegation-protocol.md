# Multi-Agent Delegation Protocol

This document defines how multiple AI coding agents — Claude Code, Antigravity, and OpenCode — collaborate on the `agents-system` repository. All three share Gentle AI configuration (Engram persistent memory + the SDD flow), but they run as **separate processes with no direct inter-process communication**.

Coordination is therefore **asynchronous, through shared artifacts**:

- **Git** — code and history (source of truth)
- **Engram** — shared rich context (the blackboard)
- **`delegations.md`** — the human-readable task ledger (the assignment board)

There is no real-time chatter between agents. They leave durable messages for each other in the substrate above.

---

## Role slots (decoupled from agents)

Roles are **slots**, not fixed agents. Any Gentle-AI agent can fill any slot, because every agent reads this protocol and the same shared substrate. The live mapping of slot → agent → model lives in the **Agent roster** at the top of `delegations.md` and is edited freely.

| Slot | Responsibility |
|---|---|
| **Lead / Integrator** | Designs the work, carves vertical slices, writes and maintains `delegations.md`, reviews and integrates finished branches |
| **Worker** | Executes its assigned slice in an isolated worktree, reports to Engram, updates its row status |
| **Transport** | (Always the human, Nahuel) Relays each assignment to its agent, switches the model per task, triggers integration |

Assignment is **explicit and lead-driven**. Workers do not self-claim — there is no contention, no locking. A worker only executes the row assigned to it in the roster.

### Reassigning a slot (token exhaustion, model switch)

Because slots are decoupled from agents, you reassign by **editing the roster table** in `delegations.md`. The incoming agent recovers full context from:

- **Engram**: `methodology/multi-agent-delegation` (this protocol's decisions) + all `delegations/*` task topics
- **The ledger**: current wave, statuses, and `Result` notes

This is the continuity mechanism. If the Lead runs out of tokens mid-flight, edit the roster to name a new Lead; that agent reads the two sources above and picks up planning/integration without losing state.

---

## Concurrency model: parallel isolated

Each task runs in its own **git worktree** on its own **branch**. Worktrees share the repo's object store but have independent working directories, so agents never overwrite each other's files.

```
git worktree add ../agents-system-D-001 -b feat/D-001-slug
```

- **Branch naming**: `feat/D-00X-<short-slug>` (matches the task ID in the ledger)
- **Worktree path**: `../agents-system-D-00X` (sibling of the main checkout)
- One task = one worktree = one branch. No exceptions.

### Isolation guardrails (hard rules — this is what the incident violated)

A separate terminal is **not** isolation. A separate branch is **not** isolation. The **working directory** is the unit of isolation. Three terminals on three branches collide the moment they share one directory.

- **`cd` into your worktree and stay there.** Your terminal operates only inside `../agents-system-D-00X` — never in the main checkout (`/home/nahuel/agents-system`).
- **Never `git checkout <another-branch>`** inside your worktree. One worktree stays on its one branch for the task's life.
- **Never `git reset`, `git rebase`, or force-update shared history** (`main`, or another agent's branch). A reset on a shared checkout silently destroys committed work — that is exactly how a prior D-002 attempt was lost.
- **Commit early and often** on your branch. Uncommitted work in a shared directory is the most fragile state there is.
- Merging to `main` happens only in the main checkout, only by the Lead.

### The golden rule of slicing

> **Parallel slices must touch disjoint file sets.** If two concurrent tasks edit the same file, integration will conflict.

The Lead is responsible for carving slices so their `Scope (files)` do not overlap. When overlap is unavoidable, the tasks are placed in **different waves** (see below) and sequenced.

---

## Wave model

Work is organized in **waves**. Within a wave, all tasks run in parallel. Between waves, the Lead integrates and resolves any dependency.

```
Wave 1:  D-001 ‖ D-002 ‖ D-003     (parallel, disjoint files)
            ↓ integrate to main ↓
Wave 2:  D-004 ‖ D-005             (depend on Wave 1 output)
```

A task with `Depends on: D-001` cannot start until D-001 is merged. The Lead places dependent tasks in later waves.

---

## Division axis: vertical slice × agent strength

Each task is a **vertical slice** — a feature implemented end to end (model → service → endpoint → tests) — assigned to the agent whose strengths fit best.

### Agents, models, and cost-aware routing

Each agent runs on a model the human selects **per task**. Strong models are reserved for complex slices; cheap models handle mechanical ones, to conserve budget.

| Agent | Available models | Best used for |
|---|---|---|
| **Claude Code** | (session model) | Architecture, planning, adversarial review (judgment-day), integration, cross-cutting slices |
| **Antigravity** | Gemini 3.5 Flash (high) | Large-context multi-file changes, browser/visual verification, UI, exploratory work |
| **OpenCode** | Minimax 2.7 · DeepSeek v4 Flash · DeepSeek v4 Pro · GPT 5.4 · GPT 5.5 | Switchable per task: cheap models (Minimax 2.7, DeepSeek Flash) for basic/mechanical work; GPT 5.5 for complex slices; test generation; headless runs |

The human switches OpenCode's model per delegated task.

### Complexity tier (drives model choice)

Every task carries a **complexity tier**. The Lead sets it; the human routes the model accordingly.

| Tier | Meaning | Route to |
|---|---|---|
| `low` | Mechanical, well-specified, low ambiguity | Cheap model (Minimax 2.7, DeepSeek v4 Flash) |
| `high` | Complex reasoning, design judgment, cross-cutting | Strong model (GPT 5.5, Gemini 3.5 high, Claude) |

This is the cost-reservation strategy made explicit: don't spend premium tokens on a slice a cheap model can finish.

The mapping is a starting hypothesis — reassign as you observe what each agent and model actually does well on this codebase.

---

## Worker lifecycle (every agent follows this)

### On start
1. Scan the active wave table in `delegations.md` for the row(s) where the **Agent** column matches your identity and status is `todo`; read your row and its task detail.
2. `mem_search` Engram for `delegations/<task-id>` and any referenced context.
3. Load the skill paths listed in your task's **Skills to load**.
4. Create your worktree + branch if it doesn't exist.
5. Set your row status to `in_progress`.

### During
6. Work **only within your declared file scope**. If you need to touch a file outside scope, STOP and flag it in your row as `blocked` with a note — do not silently expand scope.
7. Follow the Gentle AI SDD flow and Strict TDD (see below).

---

## Gentle AI SDD flow (mandatory)

This project runs on the Gentle AI Spec-Driven Development flow with Strict TDD. It is not optional, and it is split across the delegation roles:

| SDD phase | Owner | Where it happens |
|---|---|---|
| explore → propose → spec → design → tasks (**planning**) | Lead | When the Lead scopes a slice and writes its ledger task. The task block is the distilled spec + design + tasks for that slice. |
| **apply** | Worker | `sdd-apply` with Strict TDD — failing test first, then implement to green, then refactor. |
| **verify** | Worker | `sdd-verify` against the task's acceptance criteria, before setting `in_review`. |
| **archive** | Lead | At integration, after merge. |

Consequences:
- The Lead does NOT hand workers an unplanned task. Every ledger task carries scope, acceptance criteria, and design constraints — that is the planning output.
- A worker treats its task block as the contract and runs apply + verify on it. It does not re-plan or expand scope; if the task is underspecified, it sets the row `blocked`.
- For a substantial NEW change (not yet sliced), the Lead runs the full SDD planning (`sdd-explore`/`sdd-new`/`sdd-ff`) before writing ledger tasks.
- Strict TDD is enforced inside `apply`: no implementation before its test.

### On finish
8. Commit on your branch (conventional commits, no AI attribution).
9. Save your outcome to Engram with `topic_key: delegations/<task-id>` — what you built, decisions, gotchas, files touched.
10. Set your row status to `in_review` and fill the **Result** note (branch ready, test status, anything the integrator must know).
11. Tell the human you're done. The human notifies the Lead.

---

## Integration (Lead-owned)

When a task reaches `in_review`, the Lead:

1. Runs a fresh-context review of the branch diff (`judgment-day` or `/code-review`).
2. Verifies tests pass.
3. Merges to `main` in dependency order (earliest wave first).
4. Sets the row status to `done`.
5. Removes the worktree: `git worktree remove ../agents-system-D-00X`.

If two merged branches conflict despite disjoint scopes, the Lead resolves on `main` and records the cause in Engram so future slicing avoids it.

---

## Status lifecycle

```
todo → in_progress → in_review → done
                ↓
             blocked   (needs lead/human decision; note why)
```

---

## Engram conventions (avoid conflicts)

- Each task writes under a **distinct** `topic_key`: `delegations/<task-id>`. Distinct keys never overwrite each other.
- Workers `mem_save` their own task context; they do **not** edit another task's topic.
- The Lead reads all `delegations/*` topics to integrate and to plan the next wave.
- Shared architectural decisions still go to their canonical topic (e.g. `architecture/...`), not to a task topic.

---

## Why this works

- **No IPC needed** — git + Engram + `delegations.md` are the shared state.
- **No claim contention** — the Lead assigns; workers execute. One row, one agent.
- **No clobbering** — worktrees isolate working dirs; disjoint file scopes prevent merge pain.
- **Full auditability** — every slice has a branch, an Engram trail, and a ledger row.
- **Plays to strengths** — each agent gets the kind of work it's best at.

---

## Cross-references

- Live task ledger: `delegations.md` (repo root)
- SDD flow and phases: project CLAUDE.md
- Skill registry: `.atl/skill-registry.md`
