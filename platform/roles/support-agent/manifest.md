---
role: support-agent
version: "1.0"
extends: platform/roles/agent
tools: [knowledge_retrieval, conversation_summarizer, client_lookup, message_sender]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:knowledge_base
  - read:conversation_logs
  - read:client_registry
  - send:message
---

# Manifest: support-agent

Four tools, and every one of them reads except the last.

- `knowledge_retrieval` — the answer source. Without it this role has nothing
  to say that is grounded in anything.
- `conversation_summarizer` — a support thread is long, and an agent that has
  lost the start of it repeats questions the customer already answered.
- `client_lookup` — who is asking. Support answers differ by account.
- `message_sender` — the reply. The one outbound action, and the only reason
  this role holds a `send:` permission.

Deliberately absent: `order_writer` and `catalog_search`. Selling is
`sales-agent`'s job, and a support agent that can write an order can be talked
into writing one.

`org_context: true` because support answers depend on which organization the
customer belongs to; the base defaults it off.
