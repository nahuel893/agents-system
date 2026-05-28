---
extends: roles/simple-role
deployment: client-a
tools: [tool_alpha, tool_beta]
skills: [skill_one, skill_two]
context:
  session: true
  user_identity: false
  org_context: false
permissions: inherit
---

# Manifest override: simple-role / client-a

Uses a subset of tools (tool_alpha, tool_beta only — tool_gamma removed).
Adds two deployment-specific skills. Permissions inherited verbatim.
