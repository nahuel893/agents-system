---
extends: platform/roles/sales-agent
deployment: client-a
tools: [message_sender, catalog_search, order_writer, session_state, client_lookup]
skills: [request_structuring, query_normalization, confirmation_workflow]
context:
  session: true
  user_identity: true
  org_context: false
permissions: inherit
---

# Manifest override: sales-agent / client-a

This fixture keeps a concrete, subtractive deployment surface for loader and
factory tests without carrying any client domain content.
