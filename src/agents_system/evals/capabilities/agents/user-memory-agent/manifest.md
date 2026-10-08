---
role: user-memory-agent
extends: platform/roles/agent
version: "1.0"
tools: [remember_user_fact, list_user_facts, order_writer, escalation_notifier]
permissions:
  - read:user_memory
  - write:user_memory
  - write:orders
  - write:order_items
  - send:escalation
---

# Manifest: user-memory-agent

Fixture agent for the `user_memory` capability (#250, ADR-011). It keeps
facts about the person it talks to with two of the per-user memory tools, and
places orders with `order_writer`. The only source of the client id an order
needs is what the person said in an earlier conversation, which the platform
hands back as an untrusted-data block.
