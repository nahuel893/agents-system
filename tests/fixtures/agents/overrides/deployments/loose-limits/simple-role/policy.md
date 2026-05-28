---
extends: roles/simple-role
deployment: loose-limits
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
  max_tool_calls: 50
---

# Policy override: simple-role / loose-limits

Uses a LOOSER execution limit (max_tool_calls: 50 vs platform default 20).
This MUST raise DefinitionError after the invariant is enforced.