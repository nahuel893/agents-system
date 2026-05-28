---
extends: roles/simple-role
deployment: bad-tools
tools: [tool_alpha, tool_NONEXISTENT]
skills: []
context:
  session: true
  user_identity: false
  org_context: false
permissions: inherit
---

# Manifest override: simple-role / bad-tools (INVALID FIXTURE)

`tool_NONEXISTENT` is NOT in the parent tool surface. The loader must raise
`DefinitionError` when validating this override.
