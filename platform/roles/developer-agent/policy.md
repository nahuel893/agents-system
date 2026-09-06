---
role: developer-agent
version: "1.0"
autonomy: supervised
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
    - command_refused_by_policy
    - repeated_command_failure
---

# Policy: developer-agent

`execution_limits` are inherited from `operator-agent` — 30s and 10 tool
calls — and not restated. Restating them would create a second place to
change them and a way for the two to disagree.

`repeated_command_failure` is added to the inherited conditions. An agent
retrying a failing command with small variations is the specific loop this
role can get into, and each attempt is a subprocess. The tool-call ceiling
stops it eventually; this stops it usefully, by telling someone.
