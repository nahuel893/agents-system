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
--   * CONNECT on this database, USAGE on the schemas of the listed views, and
--     SELECT on exactly the listed views - every other table, view, sequence
--     and schema privilege it held is revoked.
--
-- The tool re-checks all of this on every call (services/db_role.py,
-- check_query_role) and refuses to run while the role can write anything or
-- read anything beyond its allowlist. Privileges granted to PUBLIC reach this
-- role too; on PostgreSQL 14 and older run
-- `REVOKE CREATE ON SCHEMA public FROM PUBLIC` (the default since 15), or the
-- check will refuse.
--
-- Usage - run as a superuser or the owner of the listed views. The password
-- and the view list come from the caller, never from this file. Pass both
-- bare: psql quotes them itself. The view list must match the tool's
-- configured `views` exactly:
--
--   psql "$ADMIN_DATABASE_URL" \
--     -v sql_password=change-me \
--     -v sql_views=reporting.sales_v,reporting.clients_v \
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

GRANT CONNECT ON DATABASE :"DBNAME" TO sql_readonly;

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
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO sql_readonly', parts[1]);
        EXECUTE format('GRANT SELECT ON %I.%I TO sql_readonly', parts[1], parts[2]);
    END LOOP;
END
$$;

COMMIT;
