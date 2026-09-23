---
extends: roles/cmdtool-role
deployment: keep-subset
tools: []
skills: []
context:
  session: true
  user_identity: false
permissions: inherit
command_tools: [check_stock]
---

# Manifest override: cmdtool-role / keep-subset

Legal fixture — restates the same single command tool the parent declares.
