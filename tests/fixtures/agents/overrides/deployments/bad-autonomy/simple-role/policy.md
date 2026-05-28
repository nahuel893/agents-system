---
extends: roles/simple-role
deployment: bad-autonomy
autonomy: full
escalation_rules:
  inherit: true
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits: inherit
---

# Policy override: simple-role / bad-autonomy (INVALID FIXTURE)

`autonomy: full` exceeds the parent ceiling of `supervised`. The loader must
raise `DefinitionError` when validating this override.
