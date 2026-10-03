---
role: summary-agent
version: "3.0"
autonomy: supervised
untrusted_input: false
escalation_rules:
  escalate_to: human
  conditions:
    - conversation_history_empty_or_malformed
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
    - content_policy_restriction
delegation_policy:
  allowed: false
  permitted_child_roles: []
  max_depth: 0
memory_policy:
  read_scope: session
  write_scope: session
  persist_conversation: false
audit_policy:
  log_tool_calls: true
  log_delegations: false
  log_escalations: true
  retention_days: null
execution_limits: null
---

# Policy: summary-agent

## autonomy

`supervised`, its parent's level. An agent folder may match or tighten its
parent's autonomy, never loosen it, so this reference agent cannot keep the
`full` the predefined `summary-agent` declares (ADR-010). Summarization stays
a read-only, non-destructive operation: the agent does not write to
operational systems and does not take actions on behalf of users.

## escalation_rules

- `conversation_history_empty_or_malformed` — the supplied conversation history
  is empty or its structure does not match the expected schema.
- `content_policy_restriction` — the summary output cannot be produced due to
  a content policy restriction triggered by the conversation content.

`required_tool_missing`, `confidence_below_threshold` and
`explicit_user_request` are inherited from `base` and `agent` and keep their
descriptions: an agent folder describes only the conditions it adds, and the
loader rejects one that rewords an inherited condition. Its `conditions:` are
added to its parent's, so restating the inherited ones is allowed but not
required.

## delegation_policy

The summary-agent does not delegate. It is a leaf node and returns the summary
artifact directly to the caller.

## memory_policy

The summary-agent reads the conversation history supplied via session context.
It does not maintain its own persistent memory — output artifacts are returned
to the caller and stored by the caller if persistence is needed.

## execution_limits

`null` — inherits all platform defaults.
