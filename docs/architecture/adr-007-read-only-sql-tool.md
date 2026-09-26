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
| **1. Database (the boundary)** | Role `sql_readonly` from `scripts/provision_sql_readonly.sql`: `LOGIN` only (no superuser, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS`, no role memberships), `default_transaction_read_only = on`, server-side timeouts, every privilege stripped, `SELECT` granted on exactly the allowlisted views. Per call, inside the same transaction and before the model's query: `SET TRANSACTION READ ONLY`; transaction-local `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, an empty `search_path` and `standard_conforming_strings = on`; `check_query_role` (read-only default, no elevated attribute, no write privilege on any relation, sequence or schema, nothing readable beyond the allowlist, every allowlisted relation a view). Always rolled back. | Any write or DDL (read-only transaction **and** missing privileges); reads beyond the allowlist (missing privileges); runaway queries (server-side timeout); grant drift after boot (per-call check). |
| **2. Application guard** (`services/sql_guard.py`) | Parsed with sqlglot's PostgreSQL dialect, never string-matched. Exactly one statement; root must be `SELECT` or a set operation of `SELECT`s; no DML, DDL, `SELECT … INTO`, `FOR UPDATE/SHARE`, data-modifying CTE or control statement anywhere in the tree. Every relation resolved by PostgreSQL's own rules (unquoted folds to lower case, quoted is exact, a CTE name shadows only inside its scope) to an allowlisted `schema.view`. Every function an unqualified name on a function allowlist; no qualified calls, `OPERATOR(...)`, bind parameters or casts to non-built-in types; only plain `'...'` string literals. Text length capped. **What executes is the canonical rendering of the validated tree** — relations schema-qualified, identifiers quoted as resolved, comments dropped — wrapped as `SELECT * FROM (…) LIMIT n + 1`, sent through the driver's extended protocol (asyncpg prepares every statement), which refuses a second statement independently. | Writes and DDL before they reach the server; multi-statement smuggling, including comments hiding a statement; relations outside the allowlist; side-effecting or SQL-evaluating functions (`pg_sleep`, `set_config`, advisory locks, `dblink`, `lo_*`, `query_to_xml`); oversized results (at most `n + 1` rows ever fetched). |
| **3. Tool surface** | Its own permission `query:sql` in a new top-level `Query` family, T2, so Layer 2 revalidates every call and no `read:*` grant covers it. Fixed error texts chosen by SQLSTATE class; none carries the SQL error, the driver exception or connection details. Not equipped by any predefined role. | Silent capability creep through an existing grant; leaking data, schema or credentials through error text. |

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
| Resource exhaustion: huge joins, sorts, `generate_series` | Transaction-local `statement_timeout`; server-side `LIMIT n + 1`; the harness's per-tool-call timeout | `statement_timeout` is settable by any session; it holds against the model because the guard refuses `SET` and `set_config`, and the role default (10 s) is a backstop |
| Grants drift after boot (someone grants `INSERT`, or `SELECT` on another view) | `check_query_role` on every call: the tool refuses to run | — |
| Error text leaking data, schema or credentials | Fixed texts per SQLSTATE class; logs keep only the SQLSTATE for query errors | — |
| Temporary tables | Refused by the read-only transaction and by the guard | The role keeps PUBLIC's `TEMPORARY` privilege; with both bypassed it could create session-private scratch tables, never touching persistent data. Optional hardening: `REVOKE TEMPORARY ON DATABASE … FROM PUBLIC` |

The strongest deployment points the tool at a physical read replica: a hot standby refuses every write regardless of role settings.

### Provisioning and wiring

1. Create the views the tool may read. Only views (or materialized views): the check refuses an allowlisted base table.
2. Provision the role as a superuser or the views' owner; the script is atomic and idempotent, and re-running it repairs drift:

   ```bash
   psql "$ADMIN_DATABASE_URL" \
     -v sql_password=change-me \
     -v sql_views=reporting.sales_v,reporting.clients_v \
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

Defaults: 100 rows per call (at most the platform's `HARD_ROW_CEILING`, 500) and a 5-second statement timeout (at most 30 s); keep it below the harness's per-tool-call timeout.

## Consequences

- AD-2's absolute form now has one documented exception, owned by this ADR; `services/reports.py` points here.
- `sqlglot` becomes a runtime dependency.
- The `bi-readonly` CI job also provisions `sql_readonly` and proves, on a real PostgreSQL, that the role refuses writes with the application guard bypassed.
- Logs carry reason codes and SQLSTATEs, not query text.

## Follow-ups

- **Which predefined roles equip `sql_query`.** None does in this change: adding a tool to a shipped role changes what importers get and is a SemVer-relevant decision of its own.
- **Live scenarios through the real application.** `tests/test_live_eval_sql_query.py` already runs the tool against a real model (an ad hoc question, and a delete request that must leave the data unchanged) through a test-only role and the direct runtime; running it through the HTTP API needs a role that declares the tool and the Phase 0 live harness (#78).
- **Re-evaluate `pglast`** if its licensing ever fits the library, to remove the parser differential at the source.
