---
role: cmdtool-role
version: "1.0"
autonomy: supervised
untrusted_input: true
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
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

# Policy: cmdtool-role

`untrusted_input: true` on purpose — this fixture proves a role holding only
`command_tools`-declared tools (permission family `run:`, not `exec:*`)
resolves without tripping ADR-002 C.11's mutual-exclusion invariant.
