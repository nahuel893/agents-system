# ADR-007 — Read-only SQL tool

**Status:** Accepted · **Date:** 2026-09-26 · **Amends:** AD-2 (report engine, D-023) · **Issue:** #80

## Summary

The platform gains `sql_query`, a tool that runs SQL **written by the model**. That is exactly what AD-2 forbids, so this ADR amends AD-2 for this one tool and moves the trust boundary: model-authored SQL runs only inside limits that **PostgreSQL itself enforces** — a dedicated role that can read nothing but an allowlist of views and write nothing at all, inside a `READ ONLY` transaction with a server-side statement timeout. An application-layer guard, a dedicated T2 permission and fixed error texts sit on top, and every layer is built to hold on its own. AD-2 stays absolute for everything else.

## Context

**Where AD-2 lives.** AD-2 is stated in the module docstring of `src/agents_system/services/reports.py` (the D-023 report engine): the platform never lets a caller — human, model or otherwise — build SQL text; each report's SQL is fixed module-level text with bound parameters, validated before anything reaches the database, and not even a table or column name is ever templated. The `run_report` tool (`connectors/report_connector.py`) is the only query path agents had, over a closed catalog of seven reports, with the `bi_readonly` role as the barrier that holds if validation and the Layer-2 interceptor both fail.

**Why change it.** The live-test plan (`docs/delivery/live-test-plan.md`) needs ad hoc, read-only questions the report catalog cannot answer, and on 2026-09-25 the owner decided to build that as a real feature: a dedicated read-only role, allowlisted views only, a row limit and a statement timeout, its own permission, no DDL/DML, a single statement, capped results, tier recommended T2.

**Numbering.** ADR-004 is library-first agents (`openspec/changes/library-first-agents/`), ADR-005 the operational-safety roadmap, and ADR-006 is tentatively reserved for delegation by the live-test plan. This is ADR-007.

## Decision

### The AD-2 amendment

- **AD-2 is unchanged for `services/reports.py`** and for every statement the platform writes itself — including this tool's own setup and catalog statements (`SET TRANSACTION READ ONLY`, `set_config(...)` with bound values, the role-check queries), which are static text.
- **One exception:** the `sql_query` tool executes SQL text authored by the model.
- **The new trust boundary:** model-authored SQL runs only inside database-enforced limits. The application may reject more, but it is never what makes a write impossible.

**Why the database, not the application, is the boundary.** The application sees text; the database executes objects. A privilege check inside PostgreSQL is evaluated on the relation, function and operator the server actually resolved, after view expansion, `search_path` lookup and every rewrite — so no parser disagreement, missed node type or guard bug can turn a read into a write. Inspecting attacker-influenced text is, at its core, a list of what is forbidden, and such lists age badly; a role's privilege set is a list of what is allowed, enforced by the component that runs the statement. And the role can be verified: the tool asks the server what its role can do on every call and refuses to run when the answer is unsafe. The application layer stays because it fails early, cheaply and with a useful message, and because it blocks what privileges cannot express (sleeping, advisory locks, runaway joins); it is the second line, not the first.

### Three layers, each meant to hold alone

| Layer | Mechanism | What it stops |
|---|---|---|
| **1. Database (the boundary)** | Role `sql_readonly` from `scripts/provision_sql_readonly.sql`: `LOGIN` only (no superuser, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS`, no role memberships), `default_transaction_read_only = on`, server-side timeouts, `temp_file_limit` (256 MB per session by default), every privilege stripped, `SELECT` granted on exactly the allowlisted views. Per call, inside the same transaction and before the model's query: `SET TRANSACTION READ ONLY`; transaction-local `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, an empty `search_path` and `standard_conforming_strings = on`; `check_query_role` (read-only default, no elevated attribute, no membership in any other role, `temp_file_limit` set and at most 1 GB, no `CREATE` on the database, no write privilege on any relation, sequence or schema, no executable `SECURITY DEFINER` function outside the system schemas, nothing readable beyond the allowlist, every allowlisted relation a materialized view or a `security_barrier` view). Always rolled back. | Any write or DDL (read-only transaction **and** missing privileges); reads beyond the allowlist (missing privileges); runaway queries (server-side timeout); temporary files filling the database host's disk (`temp_file_limit`); grant drift after boot (per-call check). |
| **2. Application guard** (`services/sql_guard.py`) | Parsed with sqlglot's PostgreSQL dialect, never string-matched. Exactly one statement; root must be `SELECT` or a set operation of `SELECT`s; no DML, DDL, `SELECT … INTO`, `FOR UPDATE/SHARE`, data-modifying CTE or control statement anywhere in the tree. Every relation resolved by PostgreSQL's own rules (unquoted folds to lower case, quoted is exact, a CTE name shadows only inside its scope) to an allowlisted `schema.view`. Every function an unqualified name on a function allowlist; no qualified calls, `OPERATOR(...)`, bind parameters or casts to non-built-in types; only plain `'...'` string literals. The guard's own work is bounded **before the parser runs**: the text is capped at 10,000 characters, then tokenized (one linear scan) and refused above 600 token reads, 16 square brackets, 20 levels of bracket or `CASE` nesting (a subscript chain `x[1][2]` counts as nesting), or 4 levels of brackets opened by a data-type keyword (`ARRAY[`, `numeric(`, `int[`, `struct(`). Every level must be closed by its own token, a bracket by its match and a `CASE` by `END`: sqlglot reads an unquoted `end` elsewhere as a column name, so an `END` that closed any level let `ARRAY[end, …` open levels the scan never counted. PostgreSQL reserves `end` (outside a `CASE` it is only a column label), so an `END` that closes no `CASE` is refused, except right after `AS`. sqlglot's parser tries such a bracket as a type and parses it again, doubling its work per level, so a token inside `k` of them counts as `2**k` reads; and it re-analyses every subscript's index as it parses it (about twenty tokens' work each), hence the square-bracket cap; the parser then runs within a 2,500-node budget that counts the nodes of abandoned attempts too, and the tree may be at most 80 levels deep. Every later check is one linear pass — function names and types are checked while the statement is rendered once — and any failure inside the guard becomes a rejection with a fixed text. The connector runs the guard on a small executor of its own, off the event loop. **What executes is the canonical rendering of the validated tree** — relations schema-qualified, identifiers quoted as resolved, comments dropped — wrapped as `SELECT * FROM (…) LIMIT n + 1`, sent through the driver's extended protocol (asyncpg prepares every statement), which refuses a second statement independently. That statement runs as a cursor behind a byte gate: the database measures every row (`octet_length` of its text form) and sends one larger than `byte_limit` as NULLs; the connector fetches 10 rows at a time, keeps the running total itself, stops at the budget, then cuts the JSON rows at the same budget and reports `truncated_bytes`. The gate keeps nothing in the database — no window, no sort: a running total over whole rows kept every row in a tuplestore that spilled to temporary files — the cursor computes only the rows fetched, and every FETCH runs on what is left of one statement timeout. The row is referenced only as `"sql_query_rows".*`, so no column of the query can shadow it. | Writes and DDL before they reach the server; multi-statement smuggling, including comments hiding a statement; relations outside the allowlist; side-effecting or SQL-evaluating functions (`pg_sleep`, `set_config`, advisory locks, `dblink`, `lo_*`, `query_to_xml`); oversized results (at most `n + 1` rows ever fetched, no row larger than `byte_limit` ever sent, and at most `byte_limit` plus one batch of 10 rows moved per call). |
| **3. Tool surface** | Its own permission `query:sql` in a new top-level `Query` family, T2, so Layer 2 revalidates every call and no `read:*` grant covers it. Fixed error texts chosen by SQLSTATE class; none carries the SQL error, the driver exception or connection details. Every other failure is a fixed result too, never an exception that aborts the turn: a value the driver cannot turn into Python (a date past year 9999, an interval of millions of years) is `value_out_of_range`, anything else `query_failed`, and the log keeps only the error class. Not equipped by any predefined role. | Silent capability creep through an existing grant; leaking data, schema or credentials through error text. |

### Parser choice

Two real parsers were considered. `pglast` wraps libpg_query — the server's own grammar — and would remove parser differentials entirely, but it is licensed GPL-3.0-or-later, and depending on it from this MIT-licensed library is a licensing decision, not a technical one. sqlglot (MIT) parses the PostgreSQL dialect well but is not the server's grammar, so a construct it reads differently from PostgreSQL is possible. The design closes that gap instead of hoping it is absent. The guard never forwards the model's text, only its rendering of the tree it validated, so what runs is what was checked — and that rendering is itself re-parsed, re-validated and required to render back to the identical text before it may run. The rendering is kept to a lexical subset on which sqlglot and PostgreSQL agree: quoted identifiers, standard strings with doubled quotes (with `standard_conforming_strings` pinned on), no comments, no dollar quoting. Escape strings (`E'...'`) and the other non-standard literal forms are refused, because review found a real breakout there: `E'\\'` (one backslash) rendered as `e'\'`, which PostgreSQL reads as an escaped quote, letting a later string literal turn into live SQL the guard never checked. The integration suite runs representative queries both ways — through the guard, and as the model's original text — and requires identical rows, so a rendering that changed a query's meaning fails CI. And the database layer holds regardless.

### Tier: T2, `query:sql`, a new `Query` family

- **Not T1.** T1 is a scoped read whose query the platform wrote (`run_report`). Here the model writes the query: its reach is every row of every allowlisted view in any shape, plus server time. T2 makes call-time revalidation structural rather than the opt-in `always_revalidate`, and a separate family keeps any `read:*` grant from covering it (R3 coverage runs through subclassing).
- **R2a / R2b.** A T2 tool requiring the T2 `query:sql` passes both. A T1 tool requiring it fails R2a (the permission outranks the tool); a T3 tool resting on it alone fails R2b (no permission reaches T3). Both are pinned by tests.
- **Not T3.** T3 means host execution; nothing here runs on the host, and every effect is bounded by database privileges. Declaring it T3 would misstate the danger and borrow R4's barrier for a reason R4 was not written for.
- **R4 consequence.** Because it is T2, R4 does not bar an `untrusted_input` role from holding `query:sql`. Granting it to such a role (a customer-facing channel) lets whoever talks to the agent steer queries over every allowlisted view, prompt injection included. Do so only when every allowlisted view is fit for that audience; otherwise give that role no `query:sql` grant.

### Threat model

| Threat | Stopped by | Residual |
|---|---|---|
| Model asked (or prompt-injected) to write, drop or alter | Guard rejects; bypassed, the read-only transaction refuses (`25006`) and the missing privileges refuse (`42501`), also inside an explicit `READ WRITE` transaction | None found; the integration suite runs each write through the role with the guard bypassed |
| Second statement smuggled (`; DROP`, line or nested block comments) | Guard's single-statement rule; comments dropped from the rendering; extended protocol refuses multiple commands; privileges | — |
| Parser differential: text the guard reads one way and PostgreSQL another (quoting, escapes, comments) | Only the guard's rendering executes, and it must re-validate to a fixed point; non-standard literals refused; `standard_conforming_strings` pinned | A differential not yet known is still bounded by the database layer: a hidden read needs a privilege the role does not have |
| Reading tables, other views or catalogs | Relation allowlist; role has `SELECT` on the allowlisted views only | `pg_catalog` stays readable to every role, as in any PostgreSQL: object and column **names** are visible, never data outside the allowlist |
| Side-effecting functions: sleeping, settings, advisory locks, files, large objects, SQL held in strings | Function allowlist; `statement_timeout`; the file and server-program functions need roles this one never gets; SQL-evaluating functions run with this role's privileges | With the guard bypassed, a session-level advisory lock would outlive the rolled-back transaction on a pooled connection — use a dedicated engine for the tool |
| Resource exhaustion: huge joins, sorts, `generate_series`; sorts and hashes of huge values spilling to temporary files on the volume that also holds the data files and the WAL | Transaction-local `statement_timeout`; server-side `LIMIT n + 1`; the harness's per-tool-call timeout; the role's `temp_file_limit` (256 MB by default), which only a superuser can change and which `check_query_role` requires to be set and at most 1 GB | `statement_timeout` is settable by any session; it holds against the model because the guard refuses `SET` and `set_config`, and the role default (10 s) is a backstop. `temp_file_limit` applies per session: every connection the tool's engine holds may write that much at once, so size the engine's pool with the disk in mind |
| Text crafted to make validation itself slow or crash it: nested `ARRAY[...]` (sqlglot's parser doubles its work per level: 33 s at 141 characters), subscript chains (quadratic), long `AND`/`OR` chains, deep nesting that exhausts Python's recursion limit | Caps checked on the token stream before the parser runs (600 token reads, where a token inside `k` brackets opened by a type keyword counts `2**k`; 16 square brackets; 20 nesting levels; 4 levels opened by a type keyword); a parser budget of 2,500 nodes including abandoned attempts; an 80-level cap on the tree; linear checks after parsing (a function's name is read from the one render of the statement, never by rendering each node on its own, which made a boolean chain quadratic); every guard failure a fixed rejection, never an exception that aborts the turn. A benchmark test runs the worst cases at the caps (typed and plain nesting, typed brackets side by side and around long lists, subscripts side by side and in chains, unary and cast chains, boolean chains, subqueries, CTEs, joins, unions, `CASE`) and each must finish in under 200 ms (the slowest takes about 25 ms on a developer machine and about three times that under coverage; a random search over 9,000 nested queries peaked at 21 ms, and one over 92,000 with `end` and `case` as filler at 15 ms) | The bound is measured, not proved: it rests on the caps and on sqlglot's parser as pinned. A thread cannot be killed, so a guard that did run long would still finish after the harness's timeout; it runs on its own small executor, so it could only delay other `sql_query` calls, never the loop's default executor |
| Oversized results inside the row cap: `rpad`, `lpad`, `string_agg`, `array_agg` put hundreds of MB in one value (up to PostgreSQL's 1 GB per value), all of which the application buffered and the harness serialized into one tool message | Byte gate: the database sends a row larger than `byte_limit` (default 64 KiB, at most 1 MiB) as NULLs; the connector fetches through a cursor 10 rows at a time and stops once the running total passes the budget, so the server computes and sends at most one batch past it; the JSON rows are cut at the same budget and the result says so (`truncated_bytes`, a fixed `note`). The gate streams: it keeps no rows, so it writes no temporary files (a running total over whole rows in the database spilled every row to disk: 2 GB for one call) | Measuring a row is server work: a query that builds huge values still costs the database up to its `statement_timeout`, never the application's memory |
| Grants drift after boot (someone grants `INSERT`, `SELECT` on another view, `CREATE` on the database, or membership in a role — `pg_execute_server_program`, `pg_read_server_files`, `pg_signal_backend` grant server-side capabilities no read-only transaction contains) | `check_query_role` on every call: the tool refuses to run; re-running the provisioning script strips the drift | — |
| A deployment-owned function reached by a qualified call (`SELECT * FROM schema.fn(...)`), for example a `SECURITY DEFINER` function that reads what the role cannot | The guard refuses schema-qualified calls in every position, `FROM` included; the empty `search_path` keeps plain names on built-ins; `check_query_role` refuses to run while the role can execute any `SECURITY DEFINER` function outside `pg_catalog` and `information_schema` | A `SECURITY INVOKER` function runs with this role's own privileges and reaches nothing more. Functions are executable by `PUBLIC` by default: a deployment with a `SECURITY DEFINER` function in a schema the role can use must `REVOKE EXECUTE … FROM PUBLIC` on it, or the tool refuses to run |
| An error oracle through a view that hides rows (a `WHERE` or `JOIN` filter): without `security_barrier` the planner may run the model's condition first, on the hidden rows, so `1 / (CASE WHEN amount > x THEN 0 ELSE 1 END)` answers "is there a hidden row above x?" (a binary search recovered a hidden maximum in 17 calls) | `check_query_role` refuses to run while an allowlisted plain view lacks `security_barrier`, and the provisioning script refuses to grant one: the view's own filters then run before any condition the query adds, except leakproof built-in operators, which cannot raise an error that depends on the data. A materialized view holds only its own rows and needs no barrier | `security_invoker` views with row-level security are not an alternative: the role would then need `SELECT` on the base tables, which the check refuses |
| Error text leaking data, schema or credentials | Fixed texts per SQLSTATE class; logs keep only the SQLSTATE for query errors | — |
| Temporary tables | Refused by the read-only transaction and by the guard | The role keeps PUBLIC's `TEMPORARY` privilege; with both bypassed it could create session-private scratch tables, never touching persistent data. Optional hardening: `REVOKE TEMPORARY ON DATABASE … FROM PUBLIC` |

The strongest deployment points the tool at a physical read replica: a hot standby refuses every write regardless of role settings.

### Provisioning and wiring

1. Create the views the tool may read. Only materialized views or `security_barrier` views (`CREATE VIEW … WITH (security_barrier)`): a view that filters rows by `WHERE` or `JOIN` only hides them from the model's conditions with that option. The script and the check refuse an allowlisted base table or plain view.
2. Provision the role as a superuser (`temp_file_limit` is a superuser setting); the script is atomic and idempotent, and re-running it repairs drift. `sql_temp_file_limit` is optional (default `256MB`):

   ```bash
   psql "$ADMIN_DATABASE_URL" \
     -v sql_password=change-me \
     -v sql_views=reporting.sales_v,reporting.clients_v \
     -v sql_temp_file_limit=256MB \
     -f scripts/provision_sql_readonly.sql
   ```

3. Build a **dedicated** engine for `sql_readonly` — never the application's read-write engine or the turn-scoped session.
4. Configure the tool with the same view list, and register it:

   ```python
   from agents_system.connectors.sql_query_connector import (
       SqlQueryConfig,
       build_sql_query_tool_spec,
   )

   config = SqlQueryConfig(
       views={
           "reporting.sales_v": "One row per invoice line: sold_on, product, units, amount.",
           "reporting.clients_v": "One row per client: id, name, region.",
       }
   )
   registry.register(build_sql_query_tool_spec(sql_engine, config))
   ```

5. At startup, `await verify_query_role(sql_engine, config.policy().allowed_relations)` (`services/db_role.py`): `False` means the role is unsafe — refuse to start or leave the tool unbound. `None` means the database could not answer; the tool re-checks on every call regardless.
6. Grant `query:sql` through `DEPLOY_GRANTS` to the roles that declare `sql_query`.

Defaults: 100 rows per call (at most the platform's `HARD_ROW_CEILING`, 500), 64 KiB of rows per call (`byte_limit`, 1 KiB to 1 MiB) and a 5-second statement timeout (at most 30 s); keep it below the harness's per-tool-call timeout.

## Consequences

- AD-2's absolute form now has one documented exception, owned by this ADR; `services/reports.py` points here.
- `sqlglot` becomes a runtime dependency.
- The `bi-readonly` CI job also provisions `sql_readonly` and proves, on a real PostgreSQL, that the role refuses writes with the application guard bypassed.
- Logs carry reason codes and SQLSTATEs, not query text.

## Follow-ups

- **Which predefined roles equip `sql_query`.** None does in this change: adding a tool to a shipped role changes what importers get and is a SemVer-relevant decision of its own.
- **Live scenarios through the real application.** `tests/test_live_eval_sql_query.py` already runs the tool against a real model (an ad hoc question, and a delete request that must leave the data unchanged) through a test-only role and the direct runtime; running it through the HTTP API needs a role that declares the tool and the Phase 0 live harness (#78).
- **Re-evaluate `pglast`** if its licensing ever fits the library, to remove the parser differential at the source.
