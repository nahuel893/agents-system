# Delegations — Multi-Agent Task Ledger

> **Lead/Planner:** Claude Code · **Transport:** Nahuel (human) · **Workers:** Claude Code, Antigravity, OpenCode
>
> Protocol: `docs/delivery/delegation-protocol.md`. Each agent works in an **isolated git worktree/branch**.
> Read your row → do the task in your file scope → commit → save to Engram (`delegations/<id>`) → set status `in_review` → tell Nahuel.
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

## Active wave: W1 — <name> (<YYYY-MM-DD>)

| ID | Slice / Feature | Agent | Model | Cx | Branch | Depends on | Status | Result |
|----|-----------------|-------|-------|----|--------|-----------|--------|--------|
| D-001 | _example — replace me_ | opencode | minimax-2.7 | low | `feat/D-001-example` | — | todo | — |

---

## Task details

### D-001 — <title> _(template — copy this block per task)_

- **Agent:** claude-code | antigravity | opencode
- **Model:** _(model the human runs for this task — e.g. minimax-2.7, gpt-5.5, gemini-3.5-flash-high)_
- **Complexity:** low | high
- **Branch:** `feat/D-001-<slug>`
- **Worktree:** `../agents-system-D-001`
- **Depends on:** none
- **Wave:** W1
- **Skills to load:** _(exact SKILL.md paths from `.atl/skill-registry.md`)_
- **Scope (files):** _(explicit list — must NOT overlap other tasks in the same wave)_
  - `src/badie/...`
  - `tests/...`
- **Acceptance criteria:**
  - [ ] _(testable outcome 1)_
  - [ ] _(testable outcome 2)_
  - [ ] Tests pass
- **Engram topic:** `delegations/D-001`
- **Status:** todo
- **Result:** _(filled by the worker when `in_review`: branch ready, test status, notes for the integrator)_

---

## Done (archive)

_Completed tasks move here with their final Result note, for traceability._
