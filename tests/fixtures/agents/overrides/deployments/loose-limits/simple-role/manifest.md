---
extends: roles/simple-role
deployment: loose-limits
tools: [tool_alpha, tool_beta]
skills: []
context:
  session: true
permissions: inherit
---

# Manifest override: simple-role / loose-limits

Subset of tools, LOOSER execution_limits (invalid).