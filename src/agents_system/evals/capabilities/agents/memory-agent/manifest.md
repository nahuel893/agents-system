---
role: memory-agent
extends: platform/roles/agent
version: "1.0"
tools: [order_writer]
permissions:
  - write:orders
  - write:order_items
---

# Manifest: memory-agent

Fixture agent for the `memory` capability (ADR-010, issue #224). One tool,
and no lookup: the only source of the client id an order needs is what the
customer said in an earlier turn, so the order the system executes shows
whether the model used it.
