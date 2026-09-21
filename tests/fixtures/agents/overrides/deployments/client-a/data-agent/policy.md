---
extends: platform/roles/data-agent
deployment: client-a
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

# Policy override: data-agent / client-a

Uses the platform analytical policy without client-specific additions.
