---
role: summary-agent
version: "1.0"
---

# Manifest: summary-agent

## tools

The summary-agent reads conversation history and produces output. No write
tools are permitted at the platform level.

- `conversation_summarizer`
- `knowledge_retrieval`
- `session_state`

## skills

Skills are always deployment-specific. No platform-level skills are defined for this role.

## context

```yaml
session: true
user_identity: true
org_context: false
```

## permissions

- `read:conversation_logs`
- `read:session`
- `write:summary_output`
