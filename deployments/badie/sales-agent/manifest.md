---
extends: platform/roles/sales-agent
deployment: badie
tools: [message_sender, catalog_search, order_writer, session_state, client_lookup]
skills: [order_extraction, colloquial_matching, confirm_flow]
context:
  session: true
  user_identity: true
  org_context: false
permissions: inherit
---

# Manifest override: sales-agent / BADIE

All tools listed here are present in the `platform/roles/sales-agent` allowed
surface. This deployment activates the full platform tool set — no tools are
removed for this client.

## tools (implementation notes)

- `message_sender` — WhatsApp Business API via Meta Cloud API
- `catalog_search` — pgvector + HNSW index over BADIE product catalog
- `order_writer` — PostgreSQL write to `orders` and `order_items` tables
- `session_state` — Redis, TTL 24 h, LangGraph conversation checkpointing
- `client_lookup` — PostgreSQL, E.164 phone number normalization

## skills

Active skills for this deployment. Skills live in `deployments/badie/sales-agent/skills/`.

- `order_extraction` — extracts structured order lines from colloquial Spanish text
- `colloquial_matching` — maps Argentine product nicknames to catalog entries
- `confirm_flow` — manages the two-step order confirmation interaction pattern

## permissions

`inherit` — takes the full permission set from `platform/roles/sales-agent`
verbatim. No permissions are added or removed for this deployment.
