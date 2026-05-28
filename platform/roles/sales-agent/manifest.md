---
role: sales-agent
version: "1.0"
tools: [message_sender, catalog_search, order_writer, session_state, client_lookup]
skills: []
context:
  session: true
  user_identity: true
  org_context: false
permissions:
  - read:catalog
  - read:client_registry
  - write:orders
  - write:order_items
  - read:price_lists
  - send:message
---

# Manifest: sales-agent

Allowed tools from the platform registry. A deployment selects which subset to
activate — it cannot add tools not listed here.

Skills are always deployment-specific. No platform-level skills are defined for
this role; the `skills` list is always empty at platform level.

Context requirements: the runtime must receive a session object and the resolved
user identity. Org-level context is not required at the platform level (a
deployment may narrow but not widen this).

Permissions define the RBAC surface the platform validates at injection time. All
six permissions must be present in the requesting identity's grant set for this
role to be instantiated.
