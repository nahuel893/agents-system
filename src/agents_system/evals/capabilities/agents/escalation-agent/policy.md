---
role: escalation-agent
version: "1.0"
autonomy: supervised
escalation_rules:
  conditions:
    - refund_requested
---

# Policy: escalation-agent

Supervised. An importer agent's escalation conditions are added to its
parent's, so `agent`'s three conditions still apply and this file describes
only the one it adds.

## escalation_rules

- `refund_requested` — the customer asks for a refund or for a charge to be
  reversed, of any amount. Only a person can approve one.
