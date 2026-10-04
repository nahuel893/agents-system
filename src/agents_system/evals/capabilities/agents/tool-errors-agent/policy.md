---
role: tool-errors-agent
version: "1.0"
autonomy: supervised
escalation_rules:
  conditions:
    - lookup_failed
---

# Policy: tool-errors-agent

Supervised. It adds one escalation condition, so reporting a failure is an
act the system records rather than only a sentence in the reply: a failed
lookup reaches a person through `escalation_notifier`. An importer agent's
conditions are added to its parent's, so `agent`'s three still apply.

## escalation_rules

- `lookup_failed` — a lookup the customer's request depends on fails or is
  refused, so the request cannot be completed. A person has to follow up.
