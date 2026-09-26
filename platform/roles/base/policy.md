---
role: base
version: "1.0"
autonomy: supervised
untrusted_input: false
escalation_rules:
  escalate_to: human
  conditions:
    - required_tool_missing
    - confidence_below_threshold
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
  log_delegations: true
  log_escalations: true
  retention_days: null
execution_limits: null
---

# Policy: base

`autonomy: supervised` is the floor, and it is a **ceiling for descendants**:
role-to-role inheritance is additive for capability but subtractive for
safety, so a child may match this or be stricter, never looser. A child
declaring `full` is rejected by the loader rather than merged.

`execution_limits: null` means the platform defaults apply. Same rule: a
descendant may tighten them and may not raise them.

`delegation_policy.allowed: false` is deliberate. Spawning another agent is
authority, and authority is granted per role, never inherited by default.

## escalation_rules

- `required_tool_missing` — a tool declared in the role's manifest is absent
  from the injected surface at execution time.
- `confidence_below_threshold` — the agent's confidence in its own answer or
  match falls below the threshold the active skill or deployment defines.

Both are declared at the root, not restated per role, so every descendant
inherits the same description without repeating it — see `agent/policy.md`
for why `conditions:` itself still has to be restated in full.
