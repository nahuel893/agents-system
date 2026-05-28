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

## Active wave: W1 — Harness hardening (2026-05-28)

| ID | Slice / Feature | Agent | Model | Cx | Branch | Depends on | Status | Result |
|----|-----------------|-------|-------|----|--------|-----------|--------|--------|
| D-001 | Enforce `execution_limits` stricter-only invariant in loader | opencode | minimax-2.7 | low | `feat/D-001-exec-limits-invariant` | — | todo | — |

---

## Task details

### D-001 — Enforce `execution_limits` stricter-only invariant in loader

The agent definition loader documents an invariant (#4) that a deployment override's `execution_limits` may only be **stricter or equal** to the parent — never looser. The docstring in `loader.py` claims this, but `merge()` does not enforce it: it takes an override's limits dict as-is. Close that gap with Strict TDD.

- **Agent:** opencode
- **Model:** minimax-2.7
- **Complexity:** low
- **Branch:** `feat/D-001-exec-limits-invariant`
- **Worktree:** `../agents-system-D-001`
- **Depends on:** none
- **Wave:** W1
- **Strict TDD:** ACTIVE. Test runner: `pytest` (or `uv run pytest`). Write the failing test FIRST, watch it fail, then implement.
- **Skills to load:** none required (mechanical fix; no project skill matches).
- **Scope (files):**
  - `src/badie/harness/loader.py`
  - `tests/test_harness_loader.py`
- **What to implement:**
  1. Add a module-level constant `_PLATFORM_DEFAULT_LIMITS: dict[str, int]` in `loader.py` with the platform defaults from `docs/platform/policy.md`, using these exact snake_case keys:
     - `tool_call_timeout_s: 10`
     - `total_execution_timeout_s: 60`
     - `max_tool_calls: 20`
     - `max_delegation_depth: 2`
     - `max_clarification_attempts: 3`
  2. Add `_validate_execution_limits(baseline: dict, override_limits: dict) -> None`. For each key present in `override_limits`, its numeric value must be `<=` the baseline value for that key. If any value is greater (looser), raise `DefinitionError` with a precise message naming the field, the override value, and the baseline.
  3. In `merge()`, where `execution_limits` is resolved: when the override provides a dict, compute `baseline = generic.execution_limits if it is a dict else _PLATFORM_DEFAULT_LIMITS`, call `_validate_execution_limits(baseline, override_dict)` BEFORE accepting it, then set the resolved limits to the override dict.
  4. Keep existing behavior intact: `execution_limits: inherit`, `None`, or absent → inherit the parent/default unchanged (no validation needed).
- **Acceptance criteria:**
  - [ ] New test: override with a STRICTER limit (e.g. `max_tool_calls: 10`) merges successfully and the resolved `AgentDefinition.execution_limits["max_tool_calls"] == 10`.
  - [ ] New test: override with a LOOSER limit (e.g. `max_tool_calls: 50`) raises `DefinitionError`.
  - [ ] New test: override with `execution_limits: inherit` still inherits with no error (regression guard).
  - [ ] Full suite green: `pytest` (no regressions).
  - [ ] `mypy src/` clean, `ruff check src/badie/harness/` clean.
- **Engram topic:** `delegations/D-001` — on finish, `mem_save` with `project: "agents-system"`, `topic_key: "delegations/D-001"`: what you changed, the keys/defaults you used, test results.
- **Do NOT:** merge to `main`, touch any file outside the scope, or change the merge directive behavior for other fields.
- **Status:** todo
- **Result:** _(fill when `in_review`: branch ready, test output summary, notes for the integrator)_

---

## Done (archive)

_Completed tasks move here with their final Result note, for traceability._
