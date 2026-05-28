---
role: data-agent
version: "1.0"
---

# Manifest: data-agent

## tools

The data-agent's tool surface is read-only. No write tools are permitted at
the platform level.

- `catalog_search`
- `client_lookup`
- `knowledge_retrieval`
- `session_state`

## skills

Skills are always deployment-specific. No platform-level skills are defined for this role.

## context

```yaml
session: true
user_identity: true
org_context: true
```

## permissions

- `read:catalog`
- `read:client_registry`
- `read:knowledge_base`
- `read:reports`
- `read:session`
