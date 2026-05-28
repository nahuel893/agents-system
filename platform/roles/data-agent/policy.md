---
role: data-agent
version: "1.0"
autonomy: full
escalation_rules:
  escalate_to: human
  conditions:
    - data_source_unreachable
    - empty_result_unexpected
    - required_tool_missing
    - query_scope_exceeds_permissions
delegation_policy:
  allowed: false
  permitted_child_roles: []
  max_depth: 0
memory_policy:
  read_scope: session
  write_scope: session
  persist_conversation: false
audit_policy:
  log_tool_calls: true
  log_delegations: false
  log_escalations: true
  retention_days: null
execution_limits: null
---

# Policy: data-agent

## autonomy

`full` — all data-agent operations are read-only and reversible. The agent may
execute retrieval queries without requiring confirmation. `full` autonomy applies
only within the declared read-only tool surface — any attempt to invoke a write
tool is blocked by the interceptor regardless of autonomy level.

## escalation_rules

- `data_source_unreachable` — the required data source is unreachable after one
  retry attempt.
- `empty_result_unexpected` — the query result is empty and the requesting
  context suggests the data should exist (possible schema change or data
  pipeline failure).
- `required_tool_missing` — a tool declared in the agent's manifest is absent
  from the injected surface at execution time.
- `query_scope_exceeds_permissions` — the query scope exceeds the permissions
  present in the injected surface.

## delegation_policy

The data-agent does not delegate. It is always a leaf node and returns results
directly to the caller (orchestrator or parent agent).

## memory_policy

The data-agent operates statelessly within a session. It does not persist
learned knowledge — query results are returned to the caller and not stored in
the agent's own memory layer.

## execution_limits

`null` — inherits all platform defaults.
