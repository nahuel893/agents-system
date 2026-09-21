---
extends: platform/roles/data-agent
deployment: client-a
tools: [run_report, client_lookup, catalog_search, session_state]
skills: [report_context_disclosure, query_term_normalization]
context:
  session: true
  user_identity: true
  org_context: true
permissions: inherit
---

# Manifest override: data-agent / client-a

This fixture preserves a concrete, subtractive analytical deployment surface
without encoding client-specific vocabulary or data rules.
