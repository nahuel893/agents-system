---
role: sales-agent
version: "1.0"
autonomy: supervised
escalation_rules:
  escalate_to: human
  conditions:
    - customer_not_registered
    - ambiguous_match_after_three_attempts
    - confidence_below_threshold
    - required_tool_missing
    - customer_requests_human
    - order_exceeds_approval_threshold
delegation_policy:
  allowed: false
  permitted_child_roles: []
  max_depth: 0
memory_policy:
  read_scope: session
  write_scope: deployment
  persist_conversation: true
audit_policy:
  log_tool_calls: true
  log_delegations: false
  log_escalations: true
  retention_days: null
execution_limits: null
---

# Policy: sales-agent

## autonomy

`supervised` — the sales-agent may act autonomously for read operations (catalog
lookups, client lookups, session reads). Order persistence (`order_writer`)
requires explicit customer confirmation before execution. This level is the
maximum ceiling — deployments may restrict to `confirm` but may not elevate.

## escalation_rules

All conditions below trigger an immediate escalation to a human operator.

- `customer_not_registered` — the inbound phone number resolves to no active
  client record, or the resolved record has `active = false`.
- `ambiguous_match_after_three_attempts` — after three clarification rounds the
  agent has not reached a confident product match for the current order line.
- `confidence_below_threshold` — the semantic similarity score for the best
  catalog match falls below the threshold defined in the active skill pack.
- `required_tool_missing` — a tool declared in the agent's manifest is absent
  from the injected surface at execution time.
- `customer_requests_human` — the customer explicitly asks to speak with a human
  operator at any point during the session.
- `order_exceeds_approval_threshold` — the computed order total exceeds the
  deployment-configured approval threshold.

## delegation_policy

The sales-agent does not delegate. It operates as a leaf node in any
orchestration topology. `max_depth: 0` and `allowed: false` are invariants — a
deployment override cannot set `allowed: true` for this role.

## memory_policy

The agent reads from the current session. It writes learned customer context
(order patterns, preferences, delivery notes) at deployment scope so that
knowledge persists across sessions for the same customer and does not leak
across deployments.

## audit_policy

`retention_days: null` inherits the platform default. Deployments may set a
stricter (shorter) retention period but may not extend it beyond the platform
default.

## execution_limits

`null` — inherits all platform defaults defined in `docs/platform/policy.md`.
