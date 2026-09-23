---
extends: roles/cmdtool-role
deployment: add-extra
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

# Policy override: cmdtool-role / add-extra (INVALID FIXTURE)

Policy inherits everything — the violation is in manifest.md (extra command
tool).
