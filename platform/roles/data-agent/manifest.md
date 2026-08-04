---
role: data-agent
version: "1.1"
tools: [catalog_search, client_lookup, run_report, session_state]
skills: []
context:
  session: true
  user_identity: true
  org_context: true
permissions:
  - read:catalog
  - read:client_registry
  - read:knowledge_base
  - read:reports
  - read:session
---

# Manifest: data-agent

The data-agent's tool surface is read-only. No write tools are permitted at the
platform level. Any attempt to invoke a write tool is blocked by the interceptor
regardless of the autonomy level declared in `policy.md`.

Skills are always deployment-specific. No platform-level skills are defined for
this role.

`org_context: true` is required because the data-agent operates on
organizational knowledge bases and report data that span beyond a single session.

## tools

- `run_report` (v1.1) — runs a pre-approved, parameterised report from an
  injected catalog. It consumes the `read:reports` permission, which this role
  has always granted but no tool used. The agent selects a report by name from
  a closed enum and supplies typed parameters; it never composes SQL. See
  `src/agentsys/services/reports.py`.

`knowledge_retrieval` was declared here through v1.0 but no connector ever
existed, so `resolve_tool_surface` raised `InjectionError: Unknown tool` and
this role could not be built at all. A manifest is a promise about what the
platform can equip — listing a tool the platform cannot supply makes the whole
role unusable, not partially usable. It returns to this list when the connector
does. The `read:knowledge_base` permission is left in place: it describes the
role's authority, which is unchanged, and grants without a matching tool are
inert (the injector resolves tools, never permissions).
