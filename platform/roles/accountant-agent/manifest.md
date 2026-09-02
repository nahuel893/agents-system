---
role: accountant-agent
version: "1.0"
extends: platform/roles/agent
tools: [run_report, knowledge_retrieval]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:reports
  - read:knowledge_base
---

# Manifest: accountant-agent

Two tools, both read-only, and the absences are the design.

- `run_report` — a closed enum of pre-approved reports with typed parameters.
  The agent selects one; it never composes SQL, and `required_permissions`
  here grants nothing beyond reading them.
- `knowledge_retrieval` — the accounting policies a number has to be read
  against. A figure without its policy is trivia.

Absent on purpose: `order_writer`, `message_sender`, and anything under
`write:`. This role holds no write permission of any kind, so the injector
refuses one even if a future manifest edit names a tool that needs it.

Shares `run_report` with `data-agent`, which is fine — they differ in what
they may do with the answer, and that difference is in the permissions, not
the tool.
