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
-- A base table hides no rows, so it needs no barrier: with
-- -v sql_allow_tables=on (default off) an ordinary or partitioned table may
-- be listed too, and the role gets SELECT on exactly it. Without the flag
-- the script refuses a table, so exposing a raw table is a decision the
-- caller makes out loud; a plain view still needs the barrier either way.
-- Set the same flag on the tool (SqlQueryConfig(allow_base_tables=True)).
-- Row-level security on a listed table still applies to this role (it has
-- no BYPASSRLS and owns nothing), and a partitioned or inheritance parent
-- exposes the rows of its children through it (ADR-007).
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
-- (at most 1GB, which the tool's check enforces), and so is sql_allow_tables
-- (on or off):
--
--   psql "$ADMIN_DATABASE_URL" \
--     -v sql_password=change-me \
--     -v sql_views=reporting.sales_v,reporting.clients_v \
--     -v sql_temp_file_limit=256MB \
--     -v sql_allow_tables=off \
--     -f scripts/provision_sql_readonly.sql
--
-- Idempotent: safe to re-run, and re-running REPAIRS drift - every setting is
-- re-applied, every privilege is stripped (by its recorded grantor, not just
-- the one running this script - issue #131), and only the listed views are
-- granted again. Remove a view from the list and re-run to revoke it.
-- Atomic: everything runs in one transaction, so a bad view name, or a
-- privilege the repair could not actually strip, leaves the role exactly as
-- it was (the final check at the end raises rather than let that commit).

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

-- 3. No inherited privileges: drop membership in any other role, from
-- whichever role actually granted it. PostgreSQL 16 tracks a role
-- membership grant's real grantor, and a bare REVOKE (no GRANTED BY)
-- resolves its own implicit grantor as the CURRENT session role: a
-- membership granted by anyone else only prints a WARNING
-- ("role ... has not been granted membership in role ... by role ...")
-- and is left in place, while this script still committed (issue #131,
-- reproduced against a real PostgreSQL 16). Naming the recorded grantor
-- explicitly is what actually removes it.
DO $$
DECLARE
    membership record;
BEGIN
    FOR membership IN
        SELECT granted.rolname AS granted_role, grantor.rolname AS grantor_role
        FROM pg_auth_members AS m
        JOIN pg_roles AS granted ON granted.oid = m.roleid
        JOIN pg_roles AS member ON member.oid = m.member
        JOIN pg_roles AS grantor ON grantor.oid = m.grantor
        WHERE member.rolname = 'sql_readonly'
    LOOP
        EXECUTE format(
            'REVOKE %I FROM sql_readonly GRANTED BY %I',
            membership.granted_role, membership.grantor_role
        );
    END LOOP;
END
$$;

-- 4. Strip every relation, sequence and schema privilege, then grant SELECT
-- on exactly the listed views. The list travels through a session setting
-- because psql does not interpolate variables inside a DO body.
\if :{?sql_allow_tables}
\else
\set sql_allow_tables off
\endif
SELECT set_config('sql_readonly.views', :'sql_views', false) AS views_to_grant;
SELECT set_config('sql_readonly.allow_tables', :'sql_allow_tables', false)
    AS allow_tables;

DO $$
DECLARE
    reader_oid oid;
    schema_row record;
    class_row record;
    grantor_row record;
    entry text;
    parts text[];
    allow_tables boolean;
    listed_kind text;
BEGIN
    allow_tables := CASE lower(btrim(current_setting('sql_readonly.allow_tables')))
        WHEN 'on' THEN true
        WHEN 'off' THEN false
    END;
    IF allow_tables IS NULL THEN
        RAISE EXCEPTION 'sql_allow_tables must be on or off, got "%"',
            current_setting('sql_readonly.allow_tables');
    END IF;

    SELECT oid INTO STRICT reader_oid FROM pg_roles WHERE rolname = 'sql_readonly';

    FOR schema_row IN
        SELECT oid, nspname, nspacl
        FROM pg_namespace
        WHERE nspname NOT IN ('pg_catalog', 'information_schema')
          AND nspname NOT LIKE 'pg\_toast%'
          AND nspname NOT LIKE 'pg\_temp\_%'
    LOOP
        -- Every table, view, materialized view, foreign table and sequence
        -- in this schema: revoke every privilege sql_readonly holds on it,
        -- from EACH role that actually granted it. A blanket
        -- `REVOKE ALL ... FROM sql_readonly` (no GRANTED BY) only removes
        -- what the CURRENT session role granted directly; a role that
        -- re-granted a delegated (WITH GRANT OPTION) privilege from someone
        -- else keeps it, with no error - reproduced against a real
        -- PostgreSQL 16 (issue #131). Unlike role membership (step 3),
        -- object-privilege REVOKE's own `GRANTED BY` clause only accepts
        -- CURRENT_USER (PostgreSQL raises "grantor must be current user"
        -- for anything else, confirmed against a real PostgreSQL 16), so
        -- the actual grantor is impersonated with `SET ROLE` instead - safe
        -- here because this whole script requires a superuser, who may
        -- `SET ROLE` to any role. `aclexplode` on a NULL ACL (no privilege
        -- customized on the object) returns no rows, so a plain
        -- default-privilege object is correctly left alone here.
        FOR class_row IN
            SELECT c.oid, c.relname, c.relacl
            FROM pg_class AS c
            WHERE c.relnamespace = schema_row.oid
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
        LOOP
            FOR grantor_row IN
                SELECT DISTINCT g.rolname
                FROM aclexplode(class_row.relacl) AS a
                JOIN pg_roles AS g ON g.oid = a.grantor
                WHERE a.grantee = reader_oid
            LOOP
                EXECUTE format('SET ROLE %I', grantor_row.rolname);
                EXECUTE format(
                    'REVOKE ALL ON %I.%I FROM sql_readonly',
                    schema_row.nspname, class_row.relname
                );
                RESET ROLE;
            END LOOP;
        END LOOP;

        FOR grantor_row IN
            SELECT DISTINCT g.rolname
            FROM aclexplode(schema_row.nspacl) AS a
            JOIN pg_roles AS g ON g.oid = a.grantor
            WHERE a.grantee = reader_oid
        LOOP
            EXECUTE format('SET ROLE %I', grantor_row.rolname);
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM sql_readonly', schema_row.nspname);
            RESET ROLE;
        END LOOP;
    END LOOP;

    FOREACH entry IN ARRAY string_to_array(current_setting('sql_readonly.views'), ',')
    LOOP
        parts := parse_ident(btrim(entry));
        IF array_length(parts, 1) <> 2 THEN
            RAISE EXCEPTION 'sql_views entry "%" must be written as schema.view', entry;
        END IF;
        SELECT c.relkind::text INTO listed_kind
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = parts[1]
          AND c.relname = parts[2]
          AND c.relkind IN ('r', 'p', 'v', 'm');
        IF listed_kind IS NULL THEN
            RAISE EXCEPTION 'sql_views entry "%" is not an existing view or base table', entry;
        END IF;
        IF listed_kind IN ('r', 'p') AND NOT allow_tables THEN
            RAISE EXCEPTION 'sql_views entry "%" is a base table; pass -v sql_allow_tables=on to allow one, or list a materialized or security_barrier view over it', entry;
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

-- 5. Verify the drift repair actually held, rather than trust that every
-- REVOKE above matched every real grantor: raise (aborting the whole
-- transaction, so nothing above is committed) if sql_readonly can still do
-- anything beyond CONNECT, the listed views' SELECT and their schemas'
-- USAGE. This is the same shape of check the tool itself runs on every call
-- (services/db_role.py, check_query_role) - run here too so drift a REVOKE
-- silently failed to repair is a hard failure at provisioning time, not a
-- surprise at the tool's next call (issue #131).
DO $$
DECLARE
    leftover_membership text;
    leftover record;
BEGIN
    SELECT granted.rolname INTO leftover_membership
    FROM pg_auth_members AS m
    JOIN pg_roles AS granted ON granted.oid = m.roleid
    JOIN pg_roles AS member ON member.oid = m.member
    WHERE member.rolname = 'sql_readonly'
    LIMIT 1;
    IF leftover_membership IS NOT NULL THEN
        RAISE EXCEPTION 'sql_readonly is still a member of role "%": a REVOKE '
            'above did not remove every grant (a grantor mismatch is the '
            'usual cause on PostgreSQL 16 - see step 3). Repair it by hand '
            'and re-run this script.', leftover_membership;
    END IF;

    IF pg_catalog.has_database_privilege('sql_readonly', current_database(), 'CREATE')
    THEN
        RAISE EXCEPTION 'sql_readonly can still CREATE in database %: the '
            'REVOKE in step 2 did not hold.', current_database();
    END IF;

    FOR leftover IN
        SELECT n.nspname, c.relname, c.relkind
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%'
          AND n.nspname NOT LIKE 'pg\_temp\_%'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND (
              (
                  c.relkind = 'S'
                  AND (
                      pg_catalog.has_sequence_privilege(
                          'sql_readonly', c.oid, 'SELECT'
                      )
                      OR pg_catalog.has_sequence_privilege(
                          'sql_readonly', c.oid, 'USAGE, UPDATE'
                      )
                  )
              )
              OR (
                  c.relkind <> 'S'
                  AND (
                      (
                          pg_catalog.has_any_column_privilege(
                              'sql_readonly', c.oid, 'SELECT'
                          )
                          -- Exempt exactly the views (or, with sql_allow_tables=on, tables)
                          -- this run just granted SELECT on. Scoped to THIS match only: a write
                          -- privilege below is never exempted, even on a
                          -- listed view (issue #131 review).
                          AND NOT (
                              c.relkind IN ('r', 'p', 'v', 'm')
                              AND EXISTS (
                                  SELECT 1
                                  FROM unnest(
                                      string_to_array(current_setting('sql_readonly.views'), ',')
                                  ) AS granted_entry(name)
                                  WHERE parse_ident(btrim(granted_entry.name))
                                        = ARRAY[n.nspname::text, c.relname::text]
                              )
                          )
                      )
                      OR pg_catalog.has_any_column_privilege(
                          'sql_readonly', c.oid, 'INSERT, UPDATE, REFERENCES'
                      )
                      OR pg_catalog.has_table_privilege(
                          'sql_readonly', c.oid, 'DELETE, TRUNCATE, TRIGGER'
                      )
                  )
              )
          )
    LOOP
        RAISE EXCEPTION 'sql_readonly can still access %.% (relkind %): a '
            'REVOKE above did not hold for every grantor.',
            leftover.nspname, leftover.relname, leftover.relkind;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS n
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%'
          AND n.nspname NOT LIKE 'pg\_temp\_%'
          AND pg_catalog.has_schema_privilege('sql_readonly', n.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'sql_readonly can still CREATE in some schema: a '
            'REVOKE above did not hold for every grantor.';
    END IF;
END
$$;

COMMIT;
