---
name: restock-helper
version: "1.0"
---

# Role: restock-helper

## purpose

A custom agent (issue #84, live-test plan Phase 4) that extends the
predefined `sales-agent`, adding one narrow capability of its own: checking
whether a SKU needs restocking. Used only as this repository's own live-test
fixture for the full "define -> register -> serve -> converse -> audit ->
update" agent lifecycle -- it has no real-world purpose.

## scope

- Domain: test fixture, layered on top of `sales-agent`'s own sales-order
  scope
- Users: none (test harness only)
- Tasks: everything `sales-agent` already does, plus checking stock status
  for a SKU via the `check_stock` command tool
