---
role: operator-agent
version: "1.0"
autonomy: supervised
untrusted_input: false
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
    - command_refused_by_policy
execution_limits:
  tool_call_timeout_s: 10
  total_execution_timeout_s: 30
  max_tool_calls: 10
---

# Policy: operator-agent

`autonomy: supervised`, matching the base. This role does not get `full`, and
the reason is not symmetry: it is the only role in the tree that can change
the host, so it is the last one that should act without a human in the loop.

`execution_limits` are tightened rather than inherited. The platform defaults
allow 60s and 20 tool calls; a role that spawns processes gets half of each,
because every one of those calls is a subprocess and an agent looping on a
failing command is a fork bomb with good intentions.

`command_refused_by_policy` is an escalation condition on purpose. An agent
that hits the allowlist boundary has found the edge of what this deployment
permits, and the right response is to say so — not to look for another way
around.
