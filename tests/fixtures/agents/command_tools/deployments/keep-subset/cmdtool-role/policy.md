---
extends: roles/cmdtool-role
deployment: keep-subset
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

# Policy override: cmdtool-role / keep-subset

Policy inherits everything — this fixture only exercises `command_tools` in
manifest.md.
