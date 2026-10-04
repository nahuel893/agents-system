---
role: tool-choice-agent
extends: platform/roles/agent
version: "1.0"
tools: [knowledge_retrieval, conversation_summarizer, order_writer]
permissions:
  - read:knowledge_base
  - read:conversation_logs
  - write:orders
  - write:order_items
---

# Manifest: tool-choice-agent

Fixture agent for the `tool_choice` capability (ADR-010, issue #224). Three
tools that serve different requests, so a scenario shows whether the model
picks the one that fits and calls it with arguments the tool accepts.
`conversation_summarizer` succeeds only for a session id that exists, so a
successful call is also proof the argument was right.
