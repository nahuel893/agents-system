---
role: summary-agent
version: "1.0"
---

# Policy: summary-agent

## autonomy

```yaml
level: full
```

Summarization is a read-only, non-destructive operation. The agent does not
write to operational systems and does not take actions on behalf of users.
`full` autonomy is appropriate within the declared read-only surface.

## escalation_rules

```yaml
escalate_to: human
conditions:
  - supplied conversation history is empty or malformed
  - required tool is not present in the injected surface
  - summary output cannot be produced due to content policy restriction
```

## delegation_policy

```yaml
allowed: false
permitted_child_roles: []
max_depth: 0
```

The summary-agent does not delegate. It is a leaf node and returns the summary
artifact directly to the caller.

## memory_policy

```yaml
read_scope: session
write_scope: session
persist_conversation: false
```

The summary-agent reads the conversation history supplied via session context.
It does not maintain its own persistent memory — output artifacts are returned
to the caller and stored by the caller if persistence is needed.

## audit_policy

```yaml
log_tool_calls: true
log_delegations: false
log_escalations: true
retention_days: null
```
