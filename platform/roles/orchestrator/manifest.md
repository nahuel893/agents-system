---
role: orchestrator
version: "1.0"
---

# Manifest: orchestrator

## tools

The orchestrator's tool surface is intentionally minimal. It uses tools only
for routing decisions and identity verification — not for domain operations.

- `client_lookup`
- `session_state`
- `escalation_notifier`

## skills

Skills are always deployment-specific. No platform-level skills are defined for this role.

## context

```yaml
session: true
user_identity: true
org_context: true
```

## permissions

- `read:client_registry`
- `read:session`
- `write:session`
- `spawn:sales-agent`
- `spawn:data-agent`
- `spawn:summary-agent`
- `send:escalation`
