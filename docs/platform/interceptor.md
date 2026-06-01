# Tool Call Interceptor

The Tool Call Interceptor is the second enforcement layer of the harness. It validates every tool call at execution time, before the connector fires.

## Why two layers?

Layer 1 (the Capability Injector) runs at **build time**: it restricts which tools the model can see via `bind_tools()`. The model literally cannot request a tool outside its injected surface.

Layer 2 (this interceptor) runs at **call time**: it validates every invocation right before the connector executes. It exists to catch what Layer 1 cannot:

- Model hallucinations of out-of-scope tool names
- Prompt injection attempts that try to invoke an unauthorized connector
- Incomplete injection bugs where the surface was built incorrectly
- Permission changes that occurred between runtime instantiation and tool invocation

Both layers must be active at all times. Neither replaces the other.

## Sensitive tool revalidation

A tool is **sensitive** if any of its `required_permissions` starts with `write:` or `send:`. These tools have real-world side effects — writing records, sending messages — so their permissions are revalidated at call time, not only at injection time.

This guards against a specific attack window: in long-running sessions, a user's permissions may be revoked between the moment the runtime was built and the moment the sensitive tool is actually called.

| Tool type | Permissions checked at | `revalidated` in result |
|-----------|----------------------|------------------------|
| Non-sensitive (`read:*`) | Build time only | `False` |
| Sensitive (`write:*`, `send:*`) | Build time + call time | `True` |

## Enforcement response

There are no silent failures. Every blocked call raises `PolicyViolation` and logs a structured event.

| Scenario | Reason | Response |
|----------|--------|----------|
| Tool not in injected surface | `not_in_surface` | Block + log + raise |
| Sensitive tool, no `current_permissions` provided | `revalidation_required` | Block + log + raise |
| Sensitive tool, permissions insufficient | `permission_revoked` | Block + log + raise |

The interceptor never attempts to find an alternative tool or silently ignore the violation.

## Structured events

Three events are emitted via structlog on every call:

| Event | When emitted |
|-------|-------------|
| `interceptor.call_blocked` | Any block — includes `tool` name and `reason` |
| `interceptor.call_allowed` | Call passed all checks, connector about to execute |
| `interceptor.call_executed` | Connector returned successfully |

## API

```python
from agentsys.harness.interceptor import intercept, PolicyViolation, CallResult

result: CallResult = intercept(
    tool_name,        # name of the tool the model requested
    tool_input,       # dict of arguments to pass to the connector
    runtime,          # the EquippedRuntime whose surface is authoritative
    current_permissions=["read:catalog", "write:orders"],  # required for sensitive tools
)

result.tool_name   # str
result.output      # whatever the connector returned
result.revalidated # bool — True if sensitive revalidation was performed
```

On any violation:

```python
try:
    result = intercept(tool_name, tool_input, runtime, current_permissions=perms)
except PolicyViolation as e:
    print(e.tool_name)  # which tool was blocked
    print(e.reason)     # not_in_surface | revalidation_required | permission_revoked
```

## Position in the harness pipeline

```
Trigger
  → System Router
    → Agent Factory → EquippedRuntime (injected surface, Layer 1)
      → Agent Runtime
        → Tool Call Interceptor ← YOU ARE HERE (Layer 2)
          → Connector executes
            → Audit / Memory
```

## Implementation

- `src/agentsys/harness/interceptor.py` — implementation
- `tests/test_harness_interceptor.py` — 9 tests (Strict TDD)

## Cross-references

- Layer 1 enforcement: `docs/platform/harness.md` (Capability Injector section)
- Permission model and RBAC: `docs/architecture/permission-model.md`
- Enforcement policy and violation responses: `docs/platform/policy.md`
- Tool definitions and sensitivity: `docs/platform/tool.md`
