# Migrating a deployment's reporting views past the `agentsys_*` rename

**Breaking change.** Commit `6402f58` (`build!: rename package to
agents-system (#189)`) renamed the four read-only reporting views described in
[`README.md`](README.md) and in `sales_reports.CONTRACT_VIEWS`:

| Old name | New name |
|---|---|
| `agentsys_customers` | `agents_system_customers` |
| `agentsys_sales` | `agents_system_sales` |
| `agentsys_sale_items` | `agents_system_sale_items` |
| `agentsys_stock` | `agents_system_stock` |

`CHANGELOG.md` records this as a breaking change under the `build!` commit
above (see `docs/operations/release-process.md` for how release-please turns
a `!` commit into a changelog entry) -- this file is the migration note that
entry points to.

## Who needs this

Only a deployment that created its four reporting views under the old
`agentsys_*` names **before** that commit. These views are deployment-owned
infrastructure -- each deployment creates them directly over its own tables
(worked example: `demo/company/02_views.sql`) -- so the package rename could
not migrate them for you. Without this migration, every report tool call
against such a deployment fails at the database level with `relation
"agents_system_sales" does not exist`, surfaced only as a generic
`database_unavailable` error.

A deployment whose views were already created under the `agents_system_*`
names (or created for the first time after the rename) needs nothing from
this file.

## Running the migration

```bash
psql "$DATABASE_URL" -f demo/migrations/0001_rename_agentsys_to_agents_system_views.sql
```

Run it against the database that holds the reporting views -- typically the
application's own read replica or reporting database, per your deployment.

The script renames each view in place with `ALTER VIEW ... RENAME TO ...`, so
the view's own `SELECT` (the part that maps your schema onto the contract) is
preserved untouched; only the name changes. It is wrapped in `IF EXISTS`
guards and a single transaction, so it is safe to run once and safe to run
again: once a view's old name is gone, its rename becomes a no-op.

## Verifying it worked

```sql
SELECT viewname FROM pg_views
WHERE viewname IN (
    'agents_system_customers',
    'agents_system_sales',
    'agents_system_sale_items',
    'agents_system_stock'
);
```

should return all four rows. `demo/load_demo_company.py` runs the same kind
of check against the demo company's own database, if you want a worked
example of what "the contract is satisfied" looks like in code.
