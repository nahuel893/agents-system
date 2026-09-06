---
name: agent
version: "1.0"
---

# Role: agent

The basic agent: the smallest thing in this taxonomy that is worth deploying
on its own, and the parent every role-defined agent extends.

## purpose

Hold a conversation, keep its state across turns, and know when to stop and
ask a human. That is the floor of usefulness — an agent that cannot escalate
is one that fails silently, which is worse than one that fails.

## scope

- Domain: none. `agent` has no business it belongs to; that is what makes it
  the parent rather than a peer of `sales-agent`.
- Users: whoever the deployment points it at.
- Tasks: conversation and escalation. Anything domain-shaped belongs to a
  descendant.

## what it deliberately does not do

Touch the machine. Terminal access and filesystem reads live in
`operator-agent`, a sibling branch, because inheritance here is additive: a
capability placed on this role would be inherited by every sales and support
agent beneath it, and no manifest could take it back.

That separation is the whole reason this role and `operator-agent` are two
roles instead of one.
