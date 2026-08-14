---
extends: platform/roles/data-agent
deployment: badie
autonomy: full
escalation_rules:
  inherit: true
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits: inherit
---

# Policy override: data-agent / BADIE

## autonomy

`full` — inherits the platform ceiling and is not narrowed. Every operation
this deployment can perform is a read against a connection whose role is
`default_transaction_read_only`, so there is nothing for a confirmation step
to protect.

Worth stating plainly, because `full` reads alarming next to a production
database: autonomy is not what makes this safe. Three independent mechanisms
do, and each holds if the other two fail — the manifest equips no write tool,
the Layer-2 interceptor revalidates `run_report` at call time
(`always_revalidate=True`), and the database role cannot execute a write
statement at all.

## escalation_rules

Inherits the four platform conditions unchanged. `empty_result_unexpected` is
the one that matters most here: an analytical question that returns nothing is
far more likely to mean a broken pipeline than a true zero, and reporting a
confident zero is worse than reporting an error.

## delegation_policy

Unchanged. The data-agent is a leaf node.

## memory_policy

Unchanged from the platform definition: session scope, no persistence.
Analytical answers are derived from the database on every turn and must never
be served from the agent's own memory — a cached figure is a figure that has
silently stopped being true.

## execution_limits

`inherit`. Note these are the harness's per-turn limits, which are separate
from and additional to the database-side `statement_timeout` on the
`bi_readonly` role.
