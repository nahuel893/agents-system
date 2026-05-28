---
extends: platform/roles/sales-agent
deployment: badie
---

# Manifest override: sales-agent / BADIE

## tools

Active tools for this deployment. All tools listed here must be present in the
platform/roles/sales-agent allowed surface.

- `message_sender` — WhatsApp Business API via Meta Cloud API
- `catalog_search` — pgvector + HNSW index over BADIE product catalog
- `order_writer` — PostgreSQL write to `orders` and `order_items` tables
- `session_state` — Redis, TTL 24h, LangGraph conversation checkpointing
- `client_lookup` — PostgreSQL, E.164 phone number normalization

## skills

Active skills for this deployment. Skills live in this deployment's `skills/`
folder.

- `order_extraction`
- `colloquial_matching`
- `confirm_flow`

## context

```yaml
session: true
user_identity: true
org_context: false
```

## permissions

Inherits the full permission set from `platform/roles/sales-agent`. No
additional permissions are added or removed.
