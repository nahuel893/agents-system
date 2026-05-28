---
extends: platform/roles/sales-agent
deployment: badie
---

# Policy override: sales-agent / BADIE

## autonomy

```yaml
level: supervised
```

Inherits the platform ceiling of `supervised`. Not narrowed further for this
deployment — BADIE requires confirmation before order writes but does not need
to block every action for approval.

## escalation_rules

Inherits all escalation conditions from `platform/roles/sales-agent/policy.md`.
The following BADIE-specific condition is added:

```yaml
escalate_to: human
conditions:
  - inherited: all conditions from platform/roles/sales-agent
  - three_failed_clarification_attempts: escalate via escalation_notifier after
      three consecutive failed clarification rounds on the same order session
```

Escalation sends a notification via `escalation_notifier` to the BADIE human
operator queue. The session is preserved so the operator can continue from the
last known state.

## delegation_policy

```yaml
allowed: false
permitted_child_roles: []
max_depth: 0
```

Unchanged from platform definition. Hierarchical delegation is not part of the
BADIE Seller AI delivery scope.

## memory_policy

```yaml
read_scope: session
write_scope: deployment
persist_conversation: true
```

Conversation logs are persisted to the `conversation_logs` table per client
user. Write scope is `deployment` — client-specific knowledge does not cross
deployment boundaries.

## audit_policy

```yaml
log_tool_calls: true
log_delegations: false
log_escalations: true
retention_days: null
```

## execution_limits

Inherits platform defaults. No BADIE-specific overrides.
