---
role: summary-agent
version: "1.0"
autonomy: full
escalation_rules:
  escalate_to: human
  conditions:
    - conversation_history_empty_or_malformed
    - required_tool_missing
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

`full` — summarization is a read-only, non-destructive operation. The agent
does not write to operational systems and does not take actions on behalf of
users. `full` autonomy is appropriate within the declared read-only surface.

## escalation_rules

- `conversation_history_empty_or_malformed` — the supplied conversation history
  is empty or its structure does not match the expected schema.
- `required_tool_missing` — a tool declared in the agent's manifest is absent
  from the injected surface at execution time.
- `content_policy_restriction` — the summary output cannot be produced due to
  a content policy restriction triggered by the conversation content.

## delegation_policy

The summary-agent does not delegate. It is a leaf node and returns the summary
artifact directly to the caller.

## memory_policy

The summary-agent reads the conversation history supplied via session context.
It does not maintain its own persistent memory — output artifacts are returned
to the caller and stored by the caller if persistence is needed.

## execution_limits

`null` — inherits all platform defaults.
