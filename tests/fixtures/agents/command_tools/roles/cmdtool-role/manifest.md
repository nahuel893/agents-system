---
role: cmdtool-role
version: "1.0"
tools: []
skills: []
context:
  session: true
  user_identity: false
permissions:
  - run:check_stock
command_tools:
  - name: check_stock
    argv: ["/usr/bin/echo", "--sku", "{sku}"]
    params:
      sku:
        type: string
        pattern: "^[A-Za-z0-9_-]{1,32}$"
        max_length: 32
    tier: T2
    permission: run:check_stock
---

# Manifest: cmdtool-role

Test fixture manifest. One declarative command tool, `check_stock`, over
`/usr/bin/echo` — no registry-backed `tools`, so the whole surface comes
from `command_tools` (ADR-002 C.12).
