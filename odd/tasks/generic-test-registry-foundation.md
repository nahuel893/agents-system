# #70-B0 — Generic test registry foundation

## Goal

Establish a test-only registry factory assembled from public platform APIs, so production `connectors/stubs.py`, `build_acme_rag_registry`, and the ACME app root can later be deleted without moving client behavior into `src/`.

## Decisions

- `agentsys.main` will stop exporting the hardcoded `app`; it retains `create_app(...)` only.
- Client-dependent `chat.py`, `smoke_chat.py`, `smoke_rag.py`, and `smoke.py` will be removed in B1.
- B0 and B1 are separate reviewable PRs.

## Tasks

- [x] Add deterministic fakes and a generic `RegistryFactory` under `tests/`, assembled from public platform builders.
- [x] Migrate test-only consumers away from `build_acme_registry` / `build_acme_rag_registry` without deleting production code.
- [x] Prove generic roles and the platform registry contract continue to build with the test fixture.
- [x] Run focused/full validation.
- [x] Parent review, fresh validation, and PR delivery of B0 before starting B1 deletion.

## Boundaries

B0 must not change production `src/` or delete files. It may copy only the behavior test suites need into `tests/`; no client naming or business-domain prose is allowed in the new test fixture.

## B0 Evidence & Remaining B1 Dependencies

- **B0 Completed**:
  - Neutral fakes (`fake_catalog_search`, `fake_client_lookup`, `fake_message_sender`, `fake_session_state`) and public platform tool builders (`build_order_writer_tool_spec`, `build_report_tool_spec`, `build_knowledge_retrieval_tool_spec`, `build_conversation_summarizer_tool_spec`, `build_escalation_notifier_tool_spec`, `build_operator_tool_specs`) assembled in `tests/conftest.py` into `build_test_registry` and `TestRegistryFactory`.
  - `create_test_app` in `tests/conftest.py` now boots with `build_test_registry` by default instead of `build_acme_rag_registry`.
  - Migrated consumers in `tests/test_platform_registries.py`, `tests/test_platform_tools_integration.py`, `tests/test_data_agent_deployment.py`, `tests/test_operator_connectors.py`, and `tests/test_main.py`.
  - Preserved exact tool surface (11 platform tools), schemas, permissions, autonomy, report-catalog wiring, operator-policy, and refusal invariants.
- **Remaining B1 Dependencies** (production deletions and client-specific test suites):
  - Production code: `src/agentsys/connectors/stubs.py`, `build_acme_rag_registry` in `src/agentsys/connectors/rag_connector.py`, `_acme_registry_factory` and module-level `app = create_app(...)` in `src/agentsys/main.py`.
  - Production smoke/chat scripts: `scripts/chat.py`, `scripts/smoke_chat.py`, `scripts/smoke_rag.py`, `scripts/smoke.py`.
  - Production-specific tests inherently testing ACME code/registry to delete in B1:
    - `tests/test_connectors_stubs.py` (inherently tests `agentsys.connectors.stubs`).
    - `tests/test_catalog_rag_connector.py` lines testing `build_acme_rag_registry` (`test_embedder_resolved_once_at_build_time`, `test_registry_has_the_expected_tools_with_async_catalog`).
    - `tests/test_report_connector.py` (`test_platform_registries_offer_the_portable_sales_report_names`, `test_run_report_is_in_the_static_registry`).
    - `tests/test_main.py` (`test_acme_registry_factory_forwards_arguments_honestly`).

## Evidence

- Base: `5124a07` (PR #87 merged).
- Issue #70 absorbs #68: fakes must be test-only and no ACME composition root remains under `src/` after B1.
- Native review is unavailable in this Pi runtime; independent verification is required before delivery.
