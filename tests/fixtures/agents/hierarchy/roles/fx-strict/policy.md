---
role: fx-strict
version: "1.0"
autonomy: supervised
escalation_rules:
  escalate_to: human
delegation_policy:
  allowed: false
memory_policy:
  read_scope: session
audit_policy:
  log_tool_calls: true
execution_limits:
  total_execution_timeout_s: 10
  max_tool_calls: 3
---

# Policy: fx-strict
