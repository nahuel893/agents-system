---
extends: roles/simple-role
deployment: strict-limits
autonomy: supervised
escalation_rules:
  inherit: true
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits:
  max_tool_calls: 10
---

# Policy override: simple-role / strict-limits

Uses a stricter execution limit (max_tool_calls: 10 vs platform default 20).