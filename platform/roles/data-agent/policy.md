---
role: data-agent
version: "1.0"
---

# Policy: data-agent

## autonomy

```yaml
level: full
```

All data-agent operations are read-only and reversible. The agent may execute
retrieval queries without requiring confirmation. `full` autonomy applies only
within the declared read-only tool surface — any attempt to invoke a write tool
is blocked by the interceptor regardless of autonomy level.

## escalation_rules

```yaml
escalate_to: human
conditions:
  - required data source is unreachable after one retry
  - query result is empty and the requesting context suggests the data should
    exist (possible schema change or data pipeline failure)
  - required tool is not present in the injected surface
  - the query scope exceeds the permissions in the injected surface
```

## delegation_policy

```yaml
allowed: false
permitted_child_roles: []
max_depth: 0
```

The data-agent does not delegate. It is always a leaf node and returns results
directly to the caller (orchestrator or parent agent).

## memory_policy

```yaml
read_scope: session
write_scope: session
persist_conversation: false
```

The data-agent operates statelessly within a session. It does not persist
learned knowledge — query results are returned to the caller and not stored
in the agent's own memory layer.

## audit_policy

```yaml
log_tool_calls: true
log_delegations: false
log_escalations: true
retention_days: null
```
