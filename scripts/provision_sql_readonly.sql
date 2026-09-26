-- Provision the sql_readonly login role used by the read-only SQL tool
-- (`sql_query`, #80, ADR-007).
--
-- The model writes this tool's SQL, so THIS ROLE is the trust boundary, not
-- the application's parser: every write must be refused by PostgreSQL even if
-- the guard in services/sql_guard.py had a bug. The role ends up with:
--
--   * LOGIN and nothing else: no superuser, CREATEDB, CREATEROLE,
--     REPLICATION or BYPASSRLS, and no membership in any other role;
--   * default_transaction_read_only = on, plus server-side statement, lock and
--     idle-in-transaction timeouts as a backstop to the tool's own
--     per-transaction limits;
--   * temp_file_limit (default 256MB, per session): a sort or hash of huge
--     values stops at that much temporary disk instead of filling the volume
--     that holds the data files. Only a superuser can change it, so the role
--     cannot raise it;
--   * CONNECT on this database, USAGE on the schemas of the listed views, and
--     SELECT on exactly the listed views - every other table, view, sequence
--     and schema privilege it held is revoked.
--
-- Every listed view must be a materialized view or a security_barrier view
-- (CREATE VIEW ... WITH (security_barrier) or ALTER VIEW ... SET
-- (security_barrier = true)); the script refuses any other. Without the
-- option the planner may run the model's conditions before the view's own
-- WHERE or JOIN, on rows the view hides, and an error raised only for some
-- values tells the model what those rows hold.
--
-- The tool re-checks all of this on every call (services/db_role.py,
-- check_query_role) and refuses to run while the role can write anything or
-- read anything beyond its allowlist, while it belongs to any other role,
-- while its temporary files are uncapped (or capped above 1GB), or while it
-- can call a SECURITY DEFINER function outside the system schemas.
-- Privileges granted to PUBLIC reach this role too; on PostgreSQL 14 and
-- older run `REVOKE CREATE ON SCHEMA public FROM PUBLIC` (the default since
-- 15), and for a SECURITY DEFINER function in a schema this role can use
-- (functions are executable by PUBLIC by default) run
-- `REVOKE EXECUTE ON FUNCTION ... FROM PUBLIC` and grant it to the roles that
-- need it, or the check will refuse.
--
-- Memory is the one limit no role setting provides: PostgreSQL does not
-- bound a query's memory, and a model-written query can ask for gigabytes.
-- Run the tool against a database whose host fails an oversized allocation
-- (vm.overcommit_memory = 2, no tighter container memory limit) instead of
-- OOM-killing it, which restarts every session on the server - ideally a
-- dedicated replica (ADR-007).
--
-- Usage - run as a superuser: temp_file_limit is a superuser-only setting.
-- The password and the view list come from the caller, never from this
-- file. Pass them bare: psql quotes them itself. The view list must match
-- the tool's configured `views` exactly; sql_temp_file_limit is optional
-- (at most 1GB, which the tool's check enforces):
--
--   psql "$ADMIN_DATABASE_URL" \
--     -v sql_password=change-me \
--     -v sql_views=reporting.sales_v,reporting.clients_v \
--     -v sql_temp_file_limit=256MB \
--     -f scripts/provision_sql_readonly.sql
--
-- Idempotent: safe to re-run, and re-running REPAIRS drift - every setting is
-- re-applied, every privilege is stripped, and only the listed views are
-- granted again. Remove a view from the list and re-run to revoke it.
-- Atomic: everything runs in one transaction, so a bad view name leaves the
-- role exactly as it was.

\set ON_ERROR_STOP on

BEGIN;

-- 1. The login role itself, with every elevated attribute explicitly off.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sql_readonly') THEN
        CREATE ROLE sql_readonly LOGIN;
    END IF;
END
$$;

ALTER ROLE sql_readonly WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD :'sql_password';

-- 2. Per-role settings. default_transaction_read_only makes every transaction
-- read-only at the server. The timeouts are a backstop: the tool sets its own,
-- shorter statement_timeout inside each transaction.
ALTER ROLE sql_readonly SET default_transaction_read_only = on;
ALTER ROLE sql_readonly SET statement_timeout = '10s';
ALTER ROLE sql_readonly SET lock_timeout = '1s';
ALTER ROLE sql_readonly SET idle_in_transaction_session_timeout = '30s';
\if :{?sql_temp_file_limit}
\else
\set sql_temp_file_limit 256MB
\endif
ALTER ROLE sql_readonly SET temp_file_limit = :'sql_temp_file_limit';

GRANT CONNECT ON DATABASE :"DBNAME" TO sql_readonly;
-- CREATE on the database would let the role create schemas; the per-call
-- check refuses to run while it holds it.
REVOKE CREATE ON DATABASE :"DBNAME" FROM sql_readonly;

-- 3. No inherited privileges: drop membership in any other role.
DO $$
DECLARE
    membership record;
BEGIN
    FOR membership IN
        SELECT granted.rolname
        FROM pg_auth_members AS m
        JOIN pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_roles AS member ON member.oid = m.member
        WHERE member.rolname = 'sql_readonly'
    LOOP
        EXECUTE format('REVOKE %I FROM sql_readonly', membership.rolname);
    END LOOP;
END
$$;

-- 4. Strip every relation, sequence and schema privilege, then grant SELECT
-- on exactly the listed views. The list travels through a session setting
-- because psql does not interpolate variables inside a DO body.
SELECT set_config('sql_readonly.views', :'sql_views', false) AS views_to_grant;

DO $$
DECLARE
    schema_row record;
    entry text;
    parts text[];
BEGIN
    FOR schema_row IN
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT IN ('pg_catalog', 'information_schema')
          AND nspname NOT LIKE 'pg\_toast%'
          AND nspname NOT LIKE 'pg\_temp\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM sql_readonly',
                       schema_row.nspname);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM sql_readonly',
                       schema_row.nspname);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM sql_readonly',
                       schema_row.nspname);
    END LOOP;

    FOREACH entry IN ARRAY string_to_array(current_setting('sql_readonly.views'), ',')
    LOOP
        parts := parse_ident(btrim(entry));
        IF array_length(parts, 1) <> 2 THEN
            RAISE EXCEPTION 'sql_views entry "%" must be written as schema.view', entry;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = parts[1]
              AND c.relname = parts[2]
              AND c.relkind IN ('v', 'm')
        ) THEN
            RAISE EXCEPTION 'sql_views entry "%" is not an existing view', entry;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = parts[1]
              AND c.relname = parts[2]
              AND c.relkind = 'v'
              AND NOT coalesce(
                  (SELECT o.option_value::boolean
                   FROM pg_options_to_table(c.reloptions) AS o
                   WHERE o.option_name = 'security_barrier'),
                  false)
        ) THEN
            RAISE EXCEPTION 'sql_views entry "%" is not a security_barrier view; run ALTER VIEW %.% SET (security_barrier = true) first',
                entry, quote_ident(parts[1]), quote_ident(parts[2]);
        END IF;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO sql_readonly', parts[1]);
        EXECUTE format('GRANT SELECT ON %I.%I TO sql_readonly', parts[1], parts[2]);
    END LOOP;
END
$$;

COMMIT;
