---
role: simple-role
version: "1.0"
tools: [tool_alpha, tool_beta, tool_gamma]
skills: []
context:
  session: true
  user_identity: false
  org_context: false
permissions:
  - read:alpha
  - read:beta
  - write:gamma
---

# Manifest: simple-role

Test fixture manifest. Tools: alpha, beta, gamma. Read-only alpha and beta, write gamma.
