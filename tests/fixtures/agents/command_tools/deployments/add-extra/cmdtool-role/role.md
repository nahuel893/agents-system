---
extends: roles/cmdtool-role
deployment: add-extra
---

# Role override: cmdtool-role / add-extra (INVALID FIXTURE)

`restock_alert` is NOT declared by the parent role. The loader must raise
`DefinitionError` when validating this override (ADR-002 C.12 — a
deployment may only remove declared `command_tools`, never add one).
