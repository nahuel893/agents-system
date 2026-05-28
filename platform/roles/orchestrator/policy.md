---
role: orchestrator
version: "1.0"
---

# Policy: orchestrator

## autonomy

```yaml
level: supervised
```

The orchestrator acts autonomously for routing and role selection. It does not
execute domain operations directly. Ambiguous routing decisions and unresolvable
identity checks require escalation.

## escalation_rules

```yaml
escalate_to: human
conditions:
  - inbound identity cannot be resolved or verified
  - no role is registered for the identified domain
  - a child agent fails to instantiate after one retry
  - a child agent returns an unhandled error state
  - required tool is not present in the injected surface
  - delegation depth limit is reached without terminal state
```

## delegation_policy

```yaml
allowed: true
permitted_child_roles:
  - sales-agent
  - data-agent
  - summary-agent
max_depth: 2
```

The orchestrator is the only role that may delegate. Child roles cannot
re-delegate to one another. The orchestrator does not call business-domain
tools directly; it routes to a child role that holds the appropriate tool
surface.

## memory_policy

```yaml
read_scope: session
write_scope: session
persist_conversation: false
```

The orchestrator does not persist business conversations. It reads and writes
session routing state only. Conversation persistence is the responsibility of
the child role that conducts the interaction.

## audit_policy

```yaml
log_tool_calls: true
log_delegations: true
log_escalations: true
retention_days: null
```

All delegations must be logged. This provides the audit trail required to
reconstruct the full execution path from trigger to terminal state.
