-- Migration: agentsys_* -> agents_system_* reporting views.
--
-- Context (issue #45): commit 6402f58 (`build!: rename package to
-- agents-system (#189)`) renamed the four read-only reporting views
-- `sales_reports.CONTRACT_VIEWS` reads from. Those views are deployment-owned
-- infrastructure -- each deployment creates them directly in its own
-- database (see `demo/company/02_views.sql` for a worked example) -- so the
-- package rename could not carry them along. A deployment that created its
-- views under the old `agentsys_*` names before that commit now gets
-- `relation "agents_system_sales" does not exist` from every report tool
-- call until it runs a migration like this one.
--
-- Renamed, in order:
--   agentsys_customers   -> agents_system_customers
--   agentsys_sales       -> agents_system_sales
--   agentsys_sale_items  -> agents_system_sale_items
--   agentsys_stock       -> agents_system_stock
--
-- ALTER VIEW is used rather than DROP/CREATE so the view's own SELECT
-- (the part every deployment wrote to map its own schema onto the
-- contract) is preserved untouched -- only the name changes.
--
-- Idempotent and safe to run more than once: `IF EXISTS` makes each rename a
-- no-op once its source view is gone, which is exactly the state after the
-- rename already happened -- whether from a previous run of this script or
-- because the deployment created its views under the new names from the
-- start. Run it once, in one transaction, against the database that holds
-- the reporting views (not against the application's own database, unless
-- they are the same one).

BEGIN;

ALTER VIEW IF EXISTS agentsys_customers  RENAME TO agents_system_customers;
ALTER VIEW IF EXISTS agentsys_sales      RENAME TO agents_system_sales;
ALTER VIEW IF EXISTS agentsys_sale_items RENAME TO agents_system_sale_items;
ALTER VIEW IF EXISTS agentsys_stock      RENAME TO agents_system_stock;

COMMIT;
