---
extends: roles/simple-role
deployment: bad-autonomy
tools: [tool_alpha, tool_beta]
skills: []
context:
  session: true
  user_identity: false
  org_context: false
permissions: inherit
---

# Manifest override: simple-role / bad-autonomy (INVALID FIXTURE)

Manifest is valid. The violation is in policy.md (autonomy elevation).
