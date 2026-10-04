---
role: escalation-agent
extends: platform/roles/agent
version: "1.0"
tools: [knowledge_retrieval]
permissions:
  - read:knowledge_base
---

# Manifest: escalation-agent

Fixture agent for the `escalation` capability (ADR-010, issue #224). It
answers store questions from the knowledge base and escalates through the
`escalation_notifier` it inherits from `agent`. Its policy adds one
condition, so a scenario shows both halves of the capability: it escalates
when that condition holds, and it does not escalate a question the
knowledge base answers.
