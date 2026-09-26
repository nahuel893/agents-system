---
role: agent
version: "1.0"
autonomy: supervised
untrusted_input: false
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
execution_limits: null
---

# Policy: agent

`autonomy: supervised` matches the base rather than loosening it — a child may
only equal or tighten, and this role has no argument for more.

`escalation_rules` adds `explicit_user_request` to the inherited conditions.
Mapping directives merge key-by-key, so the parent's `escalate_to: human`
survives and only `conditions` is replaced. A customer asking for a person
gets one.

## escalation_rules

- `explicit_user_request` — the user has directly asked to speak with a
  human, or to be routed away from the agent, at any point in the
  conversation.

`required_tool_missing` and `confidence_below_threshold` keep `base`'s
description; only the condition this role actually adds needs one here.
