---
role: sales-agent
version: "1.0"
---

# Manifest: sales-agent

## tools

Allowed tools from the platform registry. Deployment selects which subset to activate.

- `message_sender`
- `catalog_search`
- `order_writer`
- `session_state`
- `client_lookup`

## skills

Skills are always deployment-specific. No platform-level skills are defined for this role.

## context

```yaml
session: true
user_identity: true
org_context: false
```

## permissions

- `read:catalog`
- `read:client_registry`
- `write:orders`
- `write:order_items`
- `read:price_lists`
- `send:message`
