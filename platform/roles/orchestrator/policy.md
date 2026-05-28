---
role: orchestrator
version: "1.0"
autonomy: supervised
escalation_rules:
  escalate_to: human
  conditions:
    - identity_unresolvable
    - no_role_for_domain
    - child_agent_instantiation_failed
    - child_agent_unhandled_error
    - required_tool_missing
    - delegation_depth_limit_reached
delegation_policy:
  allowed: true
  permitted_child_roles:
    - sales-agent
    - data-agent
    - summary-agent
  max_depth: 2
memory_policy:
  read_scope: session
  write_scope: session
  persist_conversation: false
audit_policy:
  log_tool_calls: true
  log_delegations: true
  log_escalations: true
  retention_days: null
execution_limits: null
---

# Policy: orchestrator

## autonomy

`supervised` — the orchestrator acts autonomously for routing and role
selection. It does not execute domain operations directly. Ambiguous routing
decisions and unresolvable identity checks require escalation.

## escalation_rules

- `identity_unresolvable` — the inbound trigger identity cannot be resolved or
  verified against the client registry.
- `no_role_for_domain` — the identified domain has no registered role in the
  platform registry.
- `child_agent_instantiation_failed` — a child agent fails to instantiate after
  one retry attempt.
- `child_agent_unhandled_error` — a spawned child agent returns an unhandled
  error state that the orchestrator cannot recover from.
- `required_tool_missing` — a tool declared in the orchestrator's manifest is
  absent from the injected surface at execution time.
- `delegation_depth_limit_reached` — the delegation chain has reached `max_depth`
  without reaching a terminal state.

## delegation_policy

The orchestrator is the only role that may delegate. Child roles cannot
re-delegate to one another (`max_depth: 2` caps the chain at
orchestrator → child → grandchild). The orchestrator does not call
business-domain tools directly; it routes to a child role that holds the
appropriate tool surface.

## memory_policy

The orchestrator does not persist business conversations. It reads and writes
session routing state only. Conversation persistence is the responsibility of
the child role that conducts the interaction.

## audit_policy

All delegations must be logged (`log_delegations: true`). This provides the
audit trail required to reconstruct the full execution path from trigger to
terminal state.

## execution_limits

`null` — inherits all platform defaults. The orchestrator's execution budget
covers the full delegation chain, so deployments should not increase these
limits via overrides.
