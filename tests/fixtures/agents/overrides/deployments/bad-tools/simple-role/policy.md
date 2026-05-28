---
extends: roles/simple-role
deployment: bad-tools
autonomy: supervised
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

# Policy override: simple-role / bad-tools (INVALID FIXTURE)

Policy inherits everything — the violation is in manifest.md (bad tool).
