---
role: untrusted-input-agent
version: "1.0"
autonomy: supervised
untrusted_input: true
---

# Policy: untrusted-input-agent

Supervised, and `untrusted_input: true`, as any agent that reads content it
does not control should declare: it can never be granted a T3 tool.
`order_writer` is T2, so it stays available.
