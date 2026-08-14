-- Provision the bi_readonly login role used by the BI report tool (D-023, AD-3).
--
-- This role is the guardrail that is supposed to hold even if parameter
-- validation and the Layer-2 interceptor both have bugs. Until this script
-- existed, it was a manual step described only in prose, which means nothing
-- guaranteed it had ever been run — and a BI_DATABASE_URL pointing at a
-- write-capable role looks identical to a correct one from the application's
-- side. `main.py` now asks the database directly at startup, and this script
-- is what makes the answer "on".
--
-- Usage (the password comes from the caller, never from this file):
--
--   psql "$ADMIN_DATABASE_URL" \
--     -v bi_password="'change-me'" \
--     -f scripts/provision_bi_readonly.sql
--
-- Idempotent: safe to re-run. Re-running also REPAIRS a role whose settings
-- have drifted, because every ALTER below is unconditional.

\set ON_ERROR_STOP on

-- 1. The login role itself.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_readonly') THEN
        CREATE ROLE bi_readonly LOGIN;
    END IF;
END
$$;

ALTER ROLE bi_readonly WITH PASSWORD :bi_password;

-- 2. The three per-role settings the architecture depends on.
--
-- default_transaction_read_only is the actual guarantee: it makes every
-- transaction this role opens read-only at the server, so a mutating
-- statement is refused by Postgres rather than by anything we wrote.
ALTER ROLE bi_readonly SET default_transaction_read_only = on;

-- A report that runs longer than this is a bug or an attack, not a report.
-- Without it, one pathological query holds a connection until the pool is
-- exhausted and the sales conversation path starves behind it.
ALTER ROLE bi_readonly SET statement_timeout = '30s';

-- Analytical reads should never sit waiting on a writer's lock.
ALTER ROLE bi_readonly SET lock_timeout = '5s';

-- 3. Privileges: SELECT on the analytics tables, and nothing else.
GRANT CONNECT ON DATABASE badie TO bi_readonly;
GRANT USAGE ON SCHEMA public TO bi_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO bi_readonly;

-- Tables created later (a new migration) must not silently become invisible
-- to reporting, nor silently become writable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO bi_readonly;

-- Belt and braces: revoke anything that would let this role write even if
-- default_transaction_read_only were somehow cleared for a session.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
    FROM bi_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM bi_readonly;
