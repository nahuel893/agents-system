---
role: restock-helper
extends: ../sales-agent
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

Extends the reference `sales-agent` (issue #84's own additive-inheritance
path, ADR-004; ADR-010 for the reference agent): keeps every tool,
permission and skill `sales-agent` already declares, and adds one
declarative `command_tools` entry (ADR-002 C.12) over `/usr/bin/echo` -- the
same shape `tests/fixtures/agents/command_tools/roles/cmdtool-role` already
validates -- plus its own `restock_alert` skill (`skills/restock_alert.md`
in this same folder).

`extends: ../sales-agent` is the sibling a project that copied
`examples/agents/sales-agent` next to this folder would name. This fixture
folder has no such sibling, so the tests supply the parent in Python
instead, which replaces this line (#225):
`Agent.from_folder(path, extends=Agent.from_folder("examples/agents/sales-agent"))`.
Loaded on its own, the folder fails loudly rather than resolving against
the deprecated predefined role.

`run:check_stock` is additive on top of every permission `sales-agent`
already inherits -- its own six, plus `send:escalation` from the base
`agent` role (role-to-role folds are additive for capability,
`harness/loader.py::_fold_parent_into_child`) -- never replacing them.
