---
role: data-agent
version: "3.0"
autonomy: supervised
untrusted_input: false
escalation_rules:
  escalate_to: human
  conditions:
    - data_source_unreachable
    - empty_result_unexpected
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
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

`supervised`, its parent's level. An agent folder may match or tighten its
parent's autonomy, never loosen it, so this reference agent cannot keep the
`full` the predefined `data-agent` declares (ADR-010). Its tool surface is
read-only either way: any attempt to invoke a write tool is blocked by the
interceptor regardless of autonomy level.

## escalation_rules

- `data_source_unreachable` — the required data source is unreachable after one
  retry attempt.
- `empty_result_unexpected` — the query result is empty and the requesting
  context suggests the data should exist (possible schema change or data
  pipeline failure).
- `query_scope_exceeds_permissions` — the query scope exceeds the permissions
  present in the injected surface.

`required_tool_missing`, `confidence_below_threshold` and
`explicit_user_request` are inherited from `base` and `agent` and keep their
descriptions: an agent folder describes only the conditions it adds, and the
loader rejects one that rewords an inherited condition. Its `conditions:` are
added to its parent's, so restating the inherited ones is allowed but not
required.

## delegation_policy

The data-agent does not delegate. It is always a leaf node and returns results
directly to the caller (orchestrator or parent agent).

## memory_policy

The data-agent operates statelessly within a session. It does not persist
learned knowledge — query results are returned to the caller and not stored in
the agent's own memory layer.

## execution_limits

`null` — inherits all platform defaults.
