---
extends: platform/roles/data-agent
deployment: badie
tools: [run_report, client_lookup, catalog_search, session_state]
skills: [report_disclosure, business_vocabulary]
context:
  session: true
  user_identity: true
  org_context: true
permissions: inherit
---

# Manifest override: data-agent / BADIE

Every tool listed here is present in the `platform/roles/data-agent` allowed
surface. `knowledge_retrieval` is absent from that surface (no connector
exists yet), so it cannot appear here either.

## tools (implementation notes)

- `run_report` — pre-approved parameterised reports over `clients`, `orders`
  and `order_items`, executed on a dedicated PostgreSQL connection using the
  `bi_readonly` role. The agent selects a report from a closed enum and
  supplies typed parameters; it never composes SQL.
- `client_lookup` — resolve a client by phone number when a question names a
  specific customer rather than an aggregate.
- `catalog_search` — resolve a colloquial product name to a SKU before asking
  for a product-level report. Analysts ask about "el Fernet", not about
  `BEB-010`.
- `session_state` — Redis, so a follow-up question can refine the previous
  answer instead of restarting.

## skills

- `report_disclosure` — the agent must state which order statuses and which
  time window every figure covers. Non-negotiable; see the skill for why.
- `business_vocabulary` — maps how BADIE staff phrase questions onto the
  report catalog.

## permissions

`inherit` — the full permission set from `platform/roles/data-agent`,
verbatim. Note that `read:knowledge_base` is inherited but currently inert:
it describes the role's authority, and no tool in this deployment consumes
it. That is intentional — grants without a matching tool do nothing, since
the injector resolves tools and never permissions.
