---
role: data-agent
version: "1.0"
tools: [catalog_search, client_lookup, knowledge_retrieval, session_state]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:catalog
  - read:client_registry
  - read:knowledge_base
  - read:reports
  - read:session
---

# Manifest: data-agent

The data-agent's tool surface is read-only. No write tools are permitted at the
platform level. Any attempt to invoke a write tool is blocked by the interceptor
regardless of the autonomy level declared in `policy.md`.

Skills are always deployment-specific. No platform-level skills are defined for
this role.

`org_context: true` is required because the data-agent operates on
organizational knowledge bases and report data that span beyond a single session.
