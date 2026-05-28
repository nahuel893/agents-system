---
extends: roles/simple-role
deployment: client-a
autonomy: supervised
escalation_rules:
  inherit: true
  add:
    - client_specific_condition
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits: inherit
---

# Policy override: simple-role / client-a

Inherits all platform policy. Adds one client-specific escalation condition.
