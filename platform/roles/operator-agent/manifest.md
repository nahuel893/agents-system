---
role: operator-agent
version: "1.0"
extends: platform/roles/agent
tools: [use_term, read_file]
skills: []
context:
  session: true
  user_identity: true
permissions:
  - exec:command
  - read:files
---

# Manifest: operator-agent

Two tools and two permissions, and they move together: a tool declared
without its grant is denied at build time, because the injector resolves the
surface as `role.permissions ∩ granted_permissions`.

`exec:command` and `read:files` are deliberately NOT named by any other role
in this tree. An identity that should not run commands is refused at the
injector rather than trusted not to ask, and grepping for either permission
finds every role that can reach the host.

Both tools set `always_revalidate=True`. Neither permission starts with
`write:` or `send:`, so the Layer-2 interceptor would not re-check them at
call time by default — and a command that changes the host is exactly the
sensitive read that opt-in exists for.

Inherited from `agent`: `session_state` and `escalation_notifier`. An agent
that can run commands and cannot escalate is the worst combination on offer.
