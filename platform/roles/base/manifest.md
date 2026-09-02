---
role: base
version: "1.0"
abstract: true
tools: []
skills: []
context:
  session: true
  user_identity: true
  org_context: false
permissions:
  - read:session
---

# Manifest: base

No tools. A base that grants a tool grants it to every descendant, and the
only thing every agent genuinely needs is to know which conversation it is in.

`read:session` is the one permission here for that reason: it is the grant
`session_state` consumes, and every concrete role in this tree uses it.

`org_context: false` is the safe default — a descendant that needs the
organization context turns it on, which is a narrower and more visible
decision than every agent receiving it by inheritance.
