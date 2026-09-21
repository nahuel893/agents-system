---
extends: platform/roles/sales-agent
deployment: client-a
autonomy: supervised
escalation_rules:
  inherit: true
  add:
    - unresolved_request_after_retries
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits: inherit
---

# Policy override: sales-agent / client-a

Keeps the platform safety ceiling and adds a generic escalation condition.
