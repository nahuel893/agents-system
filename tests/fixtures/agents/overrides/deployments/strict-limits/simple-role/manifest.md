---
extends: roles/simple-role
deployment: strict-limits
tools: [tool_alpha, tool_beta]
skills: []
context:
  session: true
permissions: inherit
---

# Manifest override: simple-role / strict-limits

Subset of tools, stricter execution_limits.