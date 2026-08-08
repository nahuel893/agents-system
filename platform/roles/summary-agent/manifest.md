---
role: summary-agent
version: "1.0"
tools: [conversation_summarizer, knowledge_retrieval, session_state]
skills: []
context:
  session: true
  user_identity: true
  org_context: false
permissions:
  - read:conversation_logs
  - read:knowledge_base
  - read:session
  - write:summary_output
---

# Manifest: summary-agent

The summary-agent reads conversation history and produces output. No write
tools that affect operational systems are permitted at the platform level.
`write:summary_output` is a scoped permission that only allows persisting
the summary artifact — it does not grant write access to any operational table.

Skills are always deployment-specific. No platform-level skills are defined for
this role.
