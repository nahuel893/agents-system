---
role: commands-agent
extends: platform/roles/agent
version: "1.0"
tools: []
permissions:
  - run:factor
command_tools:
  - name: factor
    argv: ["factor", "{n}"]
    params:
      n:
        type: integer
    tier: T2
    permission: run:factor
---

# Manifest: commands-agent

Fixture agent for the `commands` capability (ADR-010, issue #224). One
declarative command tool over coreutils `factor`, which prints a number
followed by its prime factors. The factors of a ten-digit product of two
five-digit primes are not something a model can produce without running the
command, so an answer that carries them is an answer built on what the
command printed.

`argv[0]` is a bare name on purpose: the loader resolves it on `$PATH` once,
when the manifest is read, so the fixture works wherever coreutils lives.
The command runs inside bubblewrap, like every command tool, so the host
needs `bwrap` too.
