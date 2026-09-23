---
extends: roles/cmdtool-role
deployment: add-extra
tools: []
skills: []
context:
  session: true
  user_identity: false
permissions: inherit
command_tools: [check_stock, restock_alert]
---

# Manifest override: cmdtool-role / add-extra (INVALID FIXTURE)

`restock_alert` is not in the parent's declared `command_tools`. Used only
to test invariant enforcement.
