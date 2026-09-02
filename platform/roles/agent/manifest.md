---
role: agent
version: "1.0"
extends: platform/roles/base
tools: [session_state, escalation_notifier]
skills: []
context:
  session: true
  user_identity: true
permissions:
  - read:session
  - send:escalation
---

# Manifest: agent

Two tools, and the argument for each is that every descendant needs it.

- `session_state` — a conversation that forgets between turns is not a
  conversation. Consumes `read:session`, inherited from `base`.
- `escalation_notifier` — the counterpart to `base`'s escalation policy. That
  policy names `human` as the escalation target and lists the conditions;
  without a tool, it describes an intention nobody can act on.

Permissions are unioned with the parent's, and both tools above need one:
`session_state` consumes `read:session` (restated here rather than relied on
from `base`, so this file is readable alone) and `escalation_notifier`
consumes `send:escalation`. A tool declared without its permission is inert —
the injector resolves the surface as `role.permissions ∩ granted`, so it is
denied at build time rather than failing at call time.

`context` restates `session` and `user_identity` from `base` rather than
relying on inheritance alone, because both are load-bearing for the two tools
above and a reader of this file should not have to open the parent to learn
whether a turn knows who it is talking to.

Tools NOT here, and why: `knowledge_retrieval` and `conversation_summarizer`
exist and are platform-generic, but not every agent has a knowledge base or a
conversation worth summarising. They stay with the roles that name them.
