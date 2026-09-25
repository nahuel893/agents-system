# Eval explicit grants (#47)

Objective: Stop silently granting role permissions to eval scenarios without weakening existing offline scenarios. Issue: #47 — Eval runner grants every role permission implicitly when a scenario declares no grant.

Scope: eval schema/runner, offline tests, eval docs and scenario-format guidance. No live evals, Ollama, GPU use, or changes to production boot. Use English technical artifacts; public repo must contain no client identifiers, emails, local absolute paths, or AI attribution.

Decision: Preserve omitted-grant compatibility via a named, documented `all-declared` runner default with an observable log event. Explicit `granted_permissions` remains a wire-name list and flows unmodified through `build_runtime`; verify both Layer 1 tool exclusion and Layer 2 denial. This avoids rewriting every existing scenario while making the compatibility behavior conspicuous.

TDD: on, explicit issue prompt; runner: `pytest` with the worktree's `src` on `PYTHONPATH` and the project virtualenv on `PATH`. Observe RED before behavior change.

Delivery strategy: ask-on-risk, forecast <400 authored diff lines for one cohesive work unit. Branch: fix/47-eval-explicit-grants (existing). Review candidate: work-unit commit, not checklist.

- [x] T1 — Implement and document explicit eval grant policy. Route: delegated multi-file writer (schema, runner, tests, bilingual docs); 4+ file exploration mapped via read-only scout. RED targeted 41 passed / 1 failed; GREEN targeted 42 passed; full offline suite and static checks passed. Work-unit commit: `5bdc0fd` (`fix(evals): make scenario grant defaults explicit and observable`). Status: done.
- [x] T2 — Publish the unit. PR: https://github.com/nahuel893/agents-system/pull/58 (`Closes #47`); all 12 CI checks passed on the initial push. This record update is the delivery work unit. Status: done.

Verification evidence: RED targeted 41 passed / 1 failed (missing default-policy log); GREEN targeted 42 passed; full offline suite 1297 passed, 93 deselected, 18 xfailed; ruff check and format passed; mypy passed across 69 files; independent verifier repeated targeted/lint/mypy, found one formatting issue corrected and rechecked; parent spot-check 1 passed. No live eval run. Native review preflight: blocked because the package-local binary is missing; no lineage created and no review claimed. Next: merge remains the owner's decision.
