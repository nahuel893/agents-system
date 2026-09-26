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

- [x] T6 — Review fixes on PR #90 (route: inline executor, one commit per finding, strict TDD). Scratch PostgreSQL 16 with the fixture and provisioned role for RED/GREEN on the integration side.
  - T6a — Schema-qualified table functions in FROM (`SELECT * FROM public.lower('x')`) passed the guard. RED 11 failed; GREEN 147 guard tests. Commit `d93999c`.
  - T6b — Guard CPU was quadratic (per-node renders; AND/OR are function nodes) and ran on the event loop: 400 terms 3.2 s, max length 30 s. Function names and types are now checked during the one render (fail-closed for any node it skips), 2,500-node budget, guard in `asyncio.to_thread`. RED 5 failed; GREEN 211; probe: 30.6 s call / 30.7 s stall became 0.0 s / 0.1 s. Commit `85b80f3`.
  - T6c — Role check ignored memberships (predefined `pg_*` roles), CREATE on the database and executable SECURITY DEFINER functions. RED 5 offline + 5 integration failed; GREEN 23 offline, 54 integration. Commit `64b6a12`.
  - T6d — Real connect failures (ConnectionRefusedError, socket.gaierror, asyncpg errors) escaped the connector and `verify_query_role`. RED 10 failed; GREEN 75 offline, 55 integration (closed-port case). Commit `023235c`.
  - T6e — Results were bounded in rows, not bytes (500 MB through one call). Byte gate in the database (running sum of row sizes, rows past `byte_limit` sent as NULLs) plus a JSON cut in the connector, `truncated_bytes`. RED 9 offline + 4 integration failed; GREEN 258 offline, 59 integration; probe: 100 x 5 MB rows now 0 rows, peak RSS 69 MiB (was 1,042 MiB). Commit `e5f2ef1`.

Acceptance: the #80 acceptance criteria. The live scenario runs through a test-only role and the direct runtime; running it through the HTTP API needs a predefined role that declares the tool and the Phase 0 harness (#78).

Progress / evidence: T1 to T4c done; T5 awaiting CI; T6 review fixes done, awaiting CI on the rebased branch.

Follow-ups noted, not in this PR: `run_report` and `role_is_read_only` have the same unwrapped-connect-error gap (pre-existing).
