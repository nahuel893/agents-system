# 309 — Live scenarios for every feature

Issue: #309 — Live scenarios for every feature: cover the 30 regression-only and uncovered rows.

## Objective

Every feature in `docs/platform/features.md` whose behavior a real model can show gets a live scenario that triggers that behavior, not just a live run that passes through its code.

## Problem

A mapping of the 84 feature rows on cc6ddb0 found 38 with direct live coverage, 18 regression-only, 12 with none and 16 with no model-facing behavior. The served-app harness configures no limits, timeout reply, retry policy or budgets, and the tool-errors scenario never makes a tool raise.

## Constraints

- Live runs use `claude-haiku-5-5` (`EVAL_PROVIDER_OVERRIDE=anthropic`) while the reference model's key is unavailable; the PR body says so.
- A scenario earns a live run only where the model's behavior is the point; an injected failure is the better proof for pure plumbing (retries, timeouts).
- The capability suite is a public promise (ADR-010): extending it updates its docs (EN + ES) and the features row.
- About 400 authored changed lines per task is a planning heuristic, not a cap.

## Delivery

Batch 1 on branch `feat/309-live-runtime-guarantees` (worktree `agents-system-worktrees/309-live-batch1`), one PR per task. Batches 2-5 follow in later branches. Strategy: ask-on-risk.

## Tasks

### Batch 1: security, permissions and runtime guarantees
- [x] T1 — Tool errors raise for real (raising fixture tool, `max_tool_errors` reply), built-in escalation default, `max_tool_calls` trip (route: delegated writer; eval suite plus tests plus docs, 2+ non-trivial files).
- [ ] T2 — Call-time interceptor with a revoked grant, memory policy deny, sandbox `/etc` refusal, context budget trim (route: delegated writer).

### Later batches (see #309)
- [ ] Batch 2: isolation and data scope (app, pg).
- [ ] Batch 3: delivery and failure (app, pg).
- [ ] Batch 4: providers and models.
- [ ] Batch 5: client overrides, catalog RAG, remaining predefined roles.

## Acceptance criteria

- Each new scenario passes its gate on Haiku with `EVAL_RUNS=3`, and the PR body reports per-scenario pass rates.
- Offline checks (`ruff check .`, `ruff format --check .`, `mypy src/`, `pytest -q`) pass.
- `docs/platform/live-eval.md` and its ES twin list the new scenarios.

## Progress

- Mapping done (delegated explorer, 2026-10-17); issue #309 opened with the gap list and batches.
- T1 done (2026-10-17, delegated writer). New scenarios: `tool_errors_raise_no_fabrication`, `tool_errors_max_errors_no_fabrication` (tool_errors capability; the fixture connector raises for any client but Bar Lucero), `escalation_builtin_default` (escalation capability; new scenario field `builtin_defaults`), `limits_max_tool_calls_boundary` (runtime scenarios, `tests/test_live_eval_runtime.py`, run on grounding-agent so the ADR-010 list of nine stays). New runner assertions `tool_errors_reached` and `reply_not_contains`; `max_tool_errors` is now an overridable limit. RED observed first (13 failing schema/runner tests), then GREEN.
  - Offline: ruff check, ruff format --check, mypy src/ clean; `pytest -q`: 3980 passed, 17 xfailed.
  - Live on `claude-haiku-5-5`, EVAL_RUNS=3, whole capabilities module plus runtime plus folder target: 20 passed; every scenario 3/3, including the four new ones. Escalation default needed a first fix: a plain "will contact you" matched the honest reply "no one will contact you", so the phrases are affirmative claims.
