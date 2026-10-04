---
role: developer-agent
version: "2.0"
autonomy: supervised
untrusted_input: false
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

`execution_limits` are all inherited from `operator-agent`: 10s per tool
call, 10 tool calls and a 30s turn budget. An agent folder may match or
tighten its parent's limits, never raise them, so this reference agent cannot
keep the 120s turn budget the predefined `developer-agent` declares
(ADR-010).

The two kinds of limit pull in different directions. The ceilings on tool
calls exist because each one is a subprocess. The turn budget also has to hold
the model's own provider calls, and 30s is below the 45s
`OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S`, so a single slow provider response can
consume the whole turn, and the served app logs
`runtime.turn_budget_below_request_timeout` at boot. Raising the budget is a
change to `operator-agent`, the parent, and the composition scenarios decided
it with live data (#225): on the reference model, no turn of
`developer-agent`'s scenarios came near 30s (see "Composition scenarios on
reference agents" in `docs/platform/live-eval.md`), so `operator-agent` keeps
it.

`repeated_command_failure` is added to the inherited conditions. An agent
retrying a failing command with small variations is the specific loop this
role can get into, and each attempt is a subprocess. The tool-call ceiling
stops it eventually; this stops it usefully, by telling someone.

## escalation_rules

- `repeated_command_failure` — the agent retried a failing command more than
  once with only small variations between attempts, instead of stopping to
  ask. Reading several candidate paths that each came back `not_found` or
  refused is the same loop: each path is a small variation of the same
  failing read.
