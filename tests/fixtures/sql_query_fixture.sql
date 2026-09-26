-- Fixture for tests/test_sql_query_integration.py (#80, ADR-007).
--
-- Loaded as the admin role BEFORE scripts/provision_sql_readonly.sql, which
-- refuses to grant a view that does not exist. Two base tables, one
-- allowlisted view over the first and one NON-allowlisted view over the
-- second: the tests prove the tool's role reads the first and nothing else.
-- Re-runnable: it drops and recreates its own schema.

\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS sql_tool_fixture CASCADE;
CREATE SCHEMA sql_tool_fixture;

CREATE TABLE sql_tool_fixture.sales (
    id integer PRIMARY KEY,
    product text NOT NULL,
    amount numeric(12, 2) NOT NULL,
    sold_on date NOT NULL
);

INSERT INTO sql_tool_fixture.sales (id, product, amount, sold_on)
SELECT g, 'product-' || (g % 5), (g * 1.25)::numeric(12, 2), date '2026-01-01' + g
FROM generate_series(1, 40) AS g;

CREATE TABLE sql_tool_fixture.secrets (id integer PRIMARY KEY, note text NOT NULL);
INSERT INTO sql_tool_fixture.secrets VALUES (1, 'never readable by the SQL tool');

-- A simple view like this one is auto-updatable: only the role's missing
-- INSERT/UPDATE/DELETE privileges stop a write through it. Every view the
-- tool may read must be a security_barrier view (the role check refuses any
-- other), so the model's predicates never run on rows a view filters out.
CREATE VIEW sql_tool_fixture.sales_v WITH (security_barrier) AS
SELECT id, product, amount, sold_on FROM sql_tool_fixture.sales;

CREATE VIEW sql_tool_fixture.secrets_v AS
SELECT id, note FROM sql_tool_fixture.secrets;
