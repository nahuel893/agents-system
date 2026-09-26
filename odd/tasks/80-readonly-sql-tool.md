# Read-only SQL tool (#80)

Objective: ship a read-only, controlled SQL tool for agents whose model-authored SQL runs only inside database-enforced limits. Issue: #80 — Add a read-only, controlled SQL tool for agents.

Problem: agents can only run the 7 catalogued `run_report` reports; ad hoc read-only questions (live-test plan Phase 1) have no path. AD-2 forbids caller-built SQL, so the tool needs an explicit amendment and a new trust boundary: the database role, not application parsing.

Scope: new connector + application-layer query guard, a `query:sql` permission (new `Query` family), role verification, a provisioning script, offline + Postgres integration tests, CI wiring, ADR-007 (EN + ES) amending AD-2, tool docs (EN + ES). Out of scope: adding the tool to any predefined role (SemVer-relevant, separate decision), writes, DDL, multi-statement execution.

Constraints: strict TDD (RED before GREEN); public repo (no client names, emails, local paths, AI attribution); fixed error texts that never interpolate driver errors or connection details; every layer must hold on its own.

TDD: on (issue prompt: "Strict TDD"). Runner: `pytest` from the worktree root with the project virtualenv on `PATH` and the worktree `src` on `PYTHONPATH`.

Delivery strategy: single PR (explicit instruction). Forecast is well above the ~400-line heuristic because tests, bilingual ADR and the integration job travel with the behavior; reported in the PR body. Branch: `feat/80-readonly-sql-tool`.

- [x] T1 — Application-layer guard (`services/sql_guard.py`): parser-based single-SELECT validation, relation and function allowlists, canonical re-rendering, row-limit wrapping. Route: inline (executor). RED: collection ImportError (module missing). GREEN: 123 guard tests. Commit `26e4f30`.
- [x] T2 — Permission family `Query` + `query:sql` (T2) and the `sql_query` connector: read-only transaction, per-statement timeout, per-call role verification, N+1 fetch, JSON-safe cells, fixed error texts. Route: inline (executor). RED: collection ImportError. GREEN: 59 connector and role tests; full offline suite green. Execution lives in `services/sql_query.py` because connectors may not call `rollback()` (static contract test). Commit `06fe05b`.
- [x] T3 — Database layer: `scripts/provision_sql_readonly.sql`, `verify_query_role`, Postgres integration test proving the role refuses writes with the application bypassed, CI wiring. RED: 34 failed / 1 passed before the role existed. GREEN: 45 passed on a throwaway PostgreSQL 16. Found and fixed: the planner evaluated `has_sequence_privilege` on a TOAST table, so privilege calls now sit behind CASE; provisioning made atomic. Commit `127832d`.
- [x] T4 — ADR-007 (EN + ES) amending AD-2, tool docs (EN + ES), `reports.py` docstring pointer, live-test-plan gap update. Docs-only plus a docstring; offline suite green.
- [x] T4b — Live scenarios (`tests/test_live_eval_sql_query.py`) through a test-only role: an ad hoc question and a delete request. 3 runs each on a local `qwen2.5:3b` and on an OpenAI-compatible model: 100% pass, data intact.
- [x] T4c — Self-review found an application-layer breakout: `E'\\'` rendered as `e'\'` let a later string become live SQL (confirmed on PostgreSQL 16; the role still refused it). Fixed: non-standard string literals refused, the rendering re-validated to a fixed point, `standard_conforming_strings` pinned. RED 7 failed / GREEN 131. Added a differential test: EXPLAIN of accepted text, as a role that could read the secret table, names only the allowlisted view's base table.
- [ ] T5 — Full offline suite, lint, format, mypy; rebase; PR; CI. Local after rebase on `c4d1ec1`: ruff check and format clean, mypy clean (73 files), 1640 passed / 17 xfailed, coverage 96.11% (gate 94), integration 49 passed. PR and CI: see the PR.

Acceptance: the #80 acceptance criteria. The live scenario runs through a test-only role and the direct runtime; running it through the HTTP API needs a predefined role that declares the tool and the Phase 0 harness (#78).

Progress / evidence: T1 to T4c done; T5 awaiting CI.
