---
role: sales-agent
version: "1.0"
---

# Policy: sales-agent

## autonomy

```yaml
level: supervised
```

The sales-agent may act autonomously for read operations (catalog lookups,
client lookups, session reads). Order persistence (`order_writer`) requires
explicit customer confirmation before execution. This level is the maximum
ceiling — deployments may restrict to `confirm` but may not elevate.

## escalation_rules

```yaml
escalate_to: human
conditions:
  - customer is not registered or account is inactive
  - ambiguous product match after three clarification attempts
  - confidence in product match falls below threshold defined in the active skill
  - required tool is not present in the injected surface
  - customer explicitly requests to speak with a human operator
  - order total exceeds the deployment-configured approval threshold
```

## delegation_policy

```yaml
allowed: false
permitted_child_roles: []
max_depth: 0
```

The sales-agent does not delegate. It operates as a leaf node in any
orchestration topology.

## memory_policy

```yaml
read_scope: session
write_scope: deployment
persist_conversation: true
```

The agent reads from the current session. It writes learned customer context
(order patterns, preferences, delivery notes) at deployment scope so that
knowledge persists across sessions for the same customer.

## audit_policy

```yaml
log_tool_calls: true
log_delegations: false
log_escalations: true
retention_days: null
```

`retention_days: null` inherits the platform default. Deployments may set a
stricter (shorter) retention period but may not extend it beyond the platform
default.
