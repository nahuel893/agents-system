---
role: orchestrator
version: "1.0"
tools: [client_lookup, session_state, escalation_notifier]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:client_registry
  - read:session
  - write:session
  - spawn:sales-agent
  - spawn:data-agent
  - spawn:summary-agent
  - send:escalation
---

# Manifest: orchestrator

The orchestrator's tool surface is intentionally minimal. It uses tools only
for routing decisions and identity verification — not for domain operations.
Domain tool calls are always delegated to child roles.

Skills are always deployment-specific. No platform-level skills are defined for
the orchestrator role.

`org_context: true` is required because the orchestrator must know the
organization context to select and route to the correct child role for the
inbound domain.

Permissions include `spawn:*` entries for each delegatable child role. The
platform evaluates these at injection time so that a misconfigured identity
cannot cause the orchestrator to spawn unauthorized roles.
