---
extends: platform/roles/sales-agent
deployment: badie
autonomy: supervised
escalation_rules:
  inherit: true
  add:
    - three_failed_clarification_attempts
delegation_policy:
  inherit: true
memory_policy:
  inherit: true
audit_policy:
  inherit: true
execution_limits: inherit
---

# Policy override: sales-agent / BADIE

## autonomy

`supervised` — inherits the platform ceiling. Not narrowed further for BADIE —
the deployment requires confirmation before order writes but does not need to
block every action for approval.

## escalation_rules

Inherits all six conditions from `platform/roles/sales-agent/policy.md` and
adds one BADIE-specific condition:

- `three_failed_clarification_attempts` — escalate via `escalation_notifier`
  after three consecutive failed clarification rounds on the same order session.
  The session is preserved so the human operator can continue from the last
  known state.

Escalation sends a notification to the BADIE human operator queue.

## delegation_policy

Unchanged from the platform definition. Hierarchical delegation is not part of
the BADIE Seller AI delivery scope.

## memory_policy

Unchanged from the platform definition. Conversation logs are persisted to the
`conversation_logs` table per client user. Write scope is `deployment` —
client-specific knowledge does not cross deployment boundaries.

## execution_limits

`inherit` — no BADIE-specific overrides. All platform defaults apply.
