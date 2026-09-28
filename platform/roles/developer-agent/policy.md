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
execution_limits:
  total_execution_timeout_s: 120
---

# Policy: developer-agent

`execution_limits` are inherited from `operator-agent` — 10s per tool call and
10 tool calls, which bound the subprocesses — except the turn budget, which
this role raises to 120s. The two are different kinds of limit. The
ceilings on tool calls exist because each one is a subprocess; the turn
budget also has to hold the model's own provider calls, and `operator-agent`'s
30s is below the 45s `OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S`, so a single slow
provider response could consume the whole turn. A turn that uses a tool makes
at least two provider calls, so the budget must hold two of them at the
request timeout (90s) plus the tool's own time: 120s. The per-tool and
tool-call ceilings are still inherited, not restated; only the key that had
to differ is declared here.

`repeated_command_failure` is added to the inherited conditions. An agent
retrying a failing command with small variations is the specific loop this
role can get into, and each attempt is a subprocess. The tool-call ceiling
stops it eventually; this stops it usefully, by telling someone.

## escalation_rules

- `repeated_command_failure` — the agent retried a failing command more than
  once with only small variations between attempts, instead of stopping to
  ask.
