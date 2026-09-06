---
role: support-agent
version: "1.0"
autonomy: supervised
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
    - confidence_below_threshold
    - explicit_user_request
    - no_knowledge_base_match
    - customer_expressed_frustration
---

# Policy: support-agent

`no_knowledge_base_match` is the condition that matters. An agent that answers
anyway when retrieval came back empty is the failure mode this whole role is
shaped to avoid — it is exactly when a model reaches for its priors and
produces something fluent and wrong.

`customer_expressed_frustration` is here because the cost of escalating early
is one human minute, and the cost of escalating late is the customer.

`autonomy: supervised`, matching the base. This role talks directly to
customers; there is no argument for less oversight.
