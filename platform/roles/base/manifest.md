---
role: base
version: "2.0"
abstract: true
tools: []
skills: []
context:
  session: true
  user_identity: true
  org_context: false
permissions: []
---

# Manifest: base

No tools. A base that grants a tool grants it to every descendant.

`org_context: false` is the safe default — a descendant that needs the
organization context turns it on, which is a narrower and more visible
decision than every agent receiving it by inheritance.
