# The demo company

A fake beverage distributor, and the answer to "every client's database is
different".

## The problem

`src/agents_system/connectors/acme_reports.py` holds six sales reports written
against `orders`, `order_items` and `clients`. They work, and they serve
exactly one company. The next one has a `facturas` table with `fecha_emision`
instead of `created_at`, statuses spelled `facturada` and `anulada`, and
invoice lines that store quantity and unit price but no line total.

Rewriting the SQL per client does not scale. Generating it from a
table-and-column mapping means splicing identifiers into SQL text, which is the
one thing `services/reports.py` exists never to do — and a mapping table cannot
express arithmetic anyway.

## The answer: four views

`src/agents_system/connectors/sales_reports.py` is one portable catalog. It reads
from four view names and nothing else:

| View | Columns |
|---|---|
| `agents_system_customers` | `customer_id`, `name`, `zone`, `segment` |
| `agents_system_sales` | `sale_id`, `sold_at`, `customer_id`, `status`, `amount` |
| `agents_system_sale_items` | `sale_id`, `sku`, `description`, `quantity`, `amount` |
| `agents_system_stock` | `sku`, `description`, `on_hand`, `reorder_point` |

A deployment writes those views once over whatever its own tables are called.
The reports never change.

The views normalize three things, and the third is the one people forget:

1. **Names** — `facturas.fecha_emision` becomes `agents_system_sales.sold_at`.
2. **Grain** — one row per sale, one row per line.
3. **Vocabulary** — `status` must be `confirmed`, `pending` or `cancelled`.
   Every status filter and every `statuses_included` disclosure depends on
   that. An unmapped value does not raise; it silently disappears from every
   filtered report.

A client who cannot create views is not stuck: `run_report` accepts any
`ReportSpec` catalog, so they supply their own — which is what
`acme_reports.py` is.

## What is in here

```
company/01_schema.sql   the fake company's own tables, deliberately different
company/02_views.sql    the four views that map it onto the contract
company/03_seed.sql     deterministic data: 12 customers, 360 invoices, 20 articles
load_demo_company.py    runs all three, then verifies the contract against the live DB
```

The seed uses no `random()`. Every value comes from modular arithmetic over a
`generate_series` index, so two runs on two machines produce identical rows and
a test can assert exact figures. Only the clock moves: invoices are dated
backwards from `now()` so the trailing-window reports have something to find.

Invoice headers are computed **from** their lines, so header revenue and line
revenue reconcile. A demo whose own numbers do not add up teaches its reader to
distrust the right answers.

## Running it

```bash
docker compose up -d
docker exec agents-system-postgres-1 psql -U postgres -c 'CREATE DATABASE agents_system_demo;'
uv run python demo/load_demo_company.py
```

Then the reports:

```bash
uv run pytest -m integration tests/test_sales_reports_integration.py -v
```

The loader refuses to run against a database holding tables it does not own —
the obvious accident here is pointing it at a real one.

## What proves the contract works

Two layers, because the cheap one cannot catch the expensive failure.

`tests/test_sales_reports_contract.py` is static: every report reads only from
contract views, binds every parameter, and carries a row limit. It runs in the
default suite. It cannot tell you the views carry the right *data*.

`tests/test_sales_reports_integration.py` runs the real reports against this
database and asserts figures derived independently from the seed — 288
confirmed, 36 pending, 36 cancelled. Both failure modes that produce
confidently wrong numbers without erroring are covered, and both were
mutation-checked: mapping `anulada` to `confirmed` kills two tests, and
dropping the quantity multiplier from the line total kills the reconciliation
test. It runs in the `demo-reports` CI job, because an integration marker
nobody runs is not coverage.
