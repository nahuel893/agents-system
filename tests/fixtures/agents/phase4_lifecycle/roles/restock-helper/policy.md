---
role: restock-helper
version: "1.0"
autonomy: supervised
execution_limits:
  max_tool_calls: 5
---

# Policy: restock-helper

`autonomy: supervised` matches `sales-agent`'s own ceiling (matching a
parent's value is always allowed). `execution_limits.max_tool_calls: 5`
TIGHTENS `sales-agent`'s own `null` (the platform default, 20) -- the one
required change issue #84 asks the custom agent to make. Every other limit
key is left unset and stays at the platform default.

`untrusted_input` is left undeclared here on purpose: this agent inherits
`sales-agent`'s own `true` (ADR-002 C.11) unchanged, which is what lets it
bind directly to the WhatsApp webhook in the live-test suite.
