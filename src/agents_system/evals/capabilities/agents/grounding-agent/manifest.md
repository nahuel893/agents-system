---
role: grounding-agent
extends: platform/roles/agent
version: "1.0"
tools: [knowledge_retrieval, order_writer]
permissions:
  - read:knowledge_base
  - write:orders
  - write:order_items
---

# Manifest: grounding-agent

Fixture agent for the `grounding` capability (ADR-010, issue #224). An order
is something the system executes, so it shows what the model took as fact:
the client id and product it places must be the ones the knowledge base
returned, and with no record there is nothing to place.
