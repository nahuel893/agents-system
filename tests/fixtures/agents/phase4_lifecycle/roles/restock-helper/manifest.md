---
role: restock-helper
extends: platform/roles/sales-agent
version: "1.0"
tools: []
skills:
  - restock_alert
context: {}
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

# Manifest: restock-helper

Extends `sales-agent` (issue #84's own additive-inheritance path, ADR-004):
keeps every tool, permission and skill `sales-agent` already declares, and
adds one declarative `command_tools` entry (ADR-002 C.12) over
`/usr/bin/echo` -- the same shape `tests/fixtures/agents/command_tools/roles/cmdtool-role`
already validates -- plus its own `restock_alert` skill (`skills/restock_alert.md`
in this same folder).

`run:check_stock` is additive on top of every permission `sales-agent`
already inherits -- its own six, plus `send:escalation` from the base
`agent` role (role-to-role folds are additive for capability,
`harness/loader.py::_fold_parent_into_child`) -- never replacing them.
