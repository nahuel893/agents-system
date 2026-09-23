---
name: base
version: "1.0"
abstract: true
---

## design notes

# Role: base

The root of the role taxonomy. It is **abstract**: `resolve("base")` raises,
because a base is a contract, not a deployable agent.

## purpose

To hold what is true of *every* agent this platform runs, so that no
descendant has to restate it and no descendant can quietly disagree with it.
Concretely: the policy floor — supervision level, escalation route, execution
ceilings — and the context every turn is entitled to.

## what does NOT belong here

A capability most descendants must not have. Role-to-role inheritance is
additive and has no removal directive, so anything placed here is granted to
every agent in the tree forever. Shell access and filesystem reads are the
obvious cases: they belong to `operator-agent`, which a role reaches only by
descending from it deliberately.

The test for whether something belongs in this file is not "is it useful" but
"would I be comfortable granting it to an agent I have not written yet".
