---
role: developer-agent
version: "2.0"
extends: ../operator-agent
tools: [knowledge_retrieval]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:knowledge_base
---

# Manifest: developer-agent

One tool declared, three inherited. That ratio is the point: `use_term` and
`read_file` come from `operator-agent`, and `escalation_notifier` from
`agent` above it, so this file is short because the taxonomy already did the
work.

`knowledge_retrieval` is the addition — conventions, runbooks, and the
decisions a repository made that its source does not explain.

Deliberately absent: `message_sender`. This role reports to whoever invoked
it, not to customers.
