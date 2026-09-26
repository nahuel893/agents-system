# Verify Report: library-first-agents

## Scope

- **Change**: `library-first-agents` (ADR-004) — widens agent-definition resolution from a single platform-role name into a three-kind `RoleLocator` (predefined role / importer folder / inline Python), adds a public `Agent` API, replaces the `{deployment}__{role}` runtime-id convention with explicit `{id: Agent}` registration, and adds Guard 1 (versioned governance of the eight predefined roles' tool/permission surface).
- **Branch/commit inspected**: `origin/main` @ `04eeb78` (worktree `lfa-archive`, branch `docs/library-first-agents-archive`, no source diff from `origin/main`).
- **Shipped PRs (all merged)**: PR1a #68 (locator types), PR1b #70 (`extends:` fail-loud rewrite), PR2 #85 (`Agent` Python API), PR3 #91 (importer skills), PR4a-i #92 (`create_app` params), PR4a-ii #95 (registration wiring), PR4b #98 (env migration + parser deletion), PR5 #101 (predefined-role rename + `examples/` relocation), PR6 #103 (Guard 1 governance), PR7 #111 (ADR-004 + ADR-002 amendments). Related closed follow-up issues: #75 (folder-containment TOCTOU, addressed by PR1b's `_resolve_within_root`), #93 (Agent API hardening, addressed by PR2's review-fix round), #104 (added the Guard 1 marker job to required status checks).
- **Artifacts inspected**: `proposal.md`, `design.md` (D1-D8), `specs/agent-definition-locator/spec.md`, `specs/agent-registration-serving/spec.md`, `specs/predefined-agent-governance/spec.md`, `tasks.md` (all PR1a-PR7 tasks checked `[x]`, `state.yaml` `pending: []`).
- **Method**: `codegraph explore`/direct read against the `lfa-archive` worktree (CodeGraph index freshly initialized for this worktree) plus `rg` over the actual shipped test files to independently confirm every requirement's `Satisfies:` citation in `tasks.md` names a test that exists and asserts the claimed behavior — not a re-statement of `tasks.md`'s own claims.
- **TDD mode**: Strict TDD was in effect for `apply` per `tasks.md`'s Cross-PR Ground Rules; this verify pass checks current shipped behavior and existing tests, not historical RED/GREEN execution (not observable after the fact).

## Commands Run

| Command | Result |
|---|---|
| `PATH=.../.venv/bin:$PATH PYTHONPATH=.../lfa-archive/src pytest -q` | **2290 passed, 184 deselected, 17 xfailed**, 7 warnings (pre-existing, unrelated to this change — `httpx`/starlette deprecation notice, SQLAlchemy savepoint warnings in health-check tests) |
| `ruff check .` | **All checks passed!** |
| `ruff format --check .` | **410 files already formatted** |
| `mypy src/` | **Success: no issues found in 73 source files** |

## Traceability: spec requirement → covering test(s) → verdict

### `agent-definition-locator`

| Requirement | Covering test(s) | Verdict |
|---|---|---|
| Agent locator discriminates exactly three source kinds | `tests/test_locator_folder.py::test_role_locator_is_importable_from_harness_loader`, `::test_bare_str_locator_isinstance_matches_role_locator`, `tests/test_locator_inline.py::test_inline_locator_is_frozen`, `::test_resolve_inline_locator_end_to_end_inherits_base_tools`, `tests/test_public_api.py` (Agent export reachability) | PASS |
| Predefined-role resolution is behavior-unchanged | `tests/test_locator_inline.py::test_platform_branch_regression_byte_identical_output`, `::test_normal_platform_chain_regression_sales_agent_extends_agent`, full `platform_role_contract.py`/`test_harness_loader.py` suite (unchanged, run green) | PASS |
| Importer-folder locator reads the same three-file contract | `tests/test_locator_folder.py::test_folder_locator_resolves_like_a_platform_role_folder`, `::test_folder_locator_missing_policy_raises_naming_file_and_folder` | PASS |
| `extends:` resolves a generic-or-predefined parent unchanged | `tests/test_extends_fail_loud.py::test_unambiguous_importer_path_succeeds`, and the PR1b-T2 folder-integration cases (bare-name / `platform/roles/<name>` forms) | PASS |
| `extends:` resolves an importer-relative parent | `tests/test_extends_fail_loud.py::test_sibling_via_dotdot_accepted`, `::test_dotdot_sibling_accepted` | PASS |
| `extends:` fails loudly when unplaceable in either locator space | `tests/test_extends_fail_loud.py::test_absolute_path_rejected`, `::test_dotdot_escape_rejected`, `::test_symlink_escape_rejected`, `::test_escape_via_dotdot_rejected`, `::test_escape_via_symlink_rejected`, `::test_absolute_rejected`, `::test_nonexistent_path_never_collapses_to_last_segment`, `::test_last_segment_collision_with_generic_agent_raises`, `::test_importer_cycle_still_detected` | PASS |
| `Agent.from_folder` produces the loader's `AgentDefinition` shape | `tests/test_agent_from_folder.py`, `tests/test_locator_folder.py::test_resolve_folder_locator_end_to_end` | PASS |
| `Agent(...)` Python-parameter construction resolves without disk | `tests/test_agent_from_params.py` | PASS |
| Folder content and Python parameters compose, with parameters as the override layer | `tests/test_agent_folder_plus_overrides.py` | PASS |
| Additive inheritance from the generic agent or any predefined role (Option B) | `tests/test_agent_extends_agent.py`, `tests/test_agent_extends_predefined_role.py` | PASS |
| The base prompt contract is appended last regardless of locator kind | `tests/test_agent_from_params.py` (PR2-T1 base-contract assertion), `tests/test_agent_folder_plus_overrides.py:123-188` | PASS — cited by behavior, not by requirement title (see Documented Deviation 1) |
| `untrusted_input` ⊥ T3-tier permission exclusion applies regardless of locator kind | `tests/test_agent_from_params.py`, `tests/test_agent_folder_plus_overrides.py:123-188` (T3-tier variant through `FolderLocator`/`InlineLocator` triggers `_validate_untrusted_input_exec`) | PASS — see Documented Deviation 1 |
| Capability tiers and the T3 sandbox gate apply regardless of locator kind | `tests/test_agent_safety_ceiling.py` (full file — importer-authored autonomy/limit ceiling), `tests/test_agent_from_params.py` | PASS — see Documented Deviation 1 |
| Skills load from an importer agent's own folder | `tests/test_skills_precedence.py::test_importer_folder_own_skills_resolves_with_no_deployment`, `::test_missing_importer_folder_skill_file_raises_naming_skill_and_path`, `::test_skill_symlink_does_not_escape_folder` | PASS |
| Skills load from inline Python-supplied content | `tests/test_skills_precedence.py::test_inline_skill_content_used_verbatim_with_no_file_read`, `::test_inline_skill_content_overrides_a_same_named_folder_skill` | PASS |
| Predefined-role deployment-only skills remain unchanged | `tests/test_skills_precedence.py::test_predefined_role_with_no_deployment_and_declared_skills_still_fails`, `::test_predefined_role_own_platform_folder_never_treated_as_skills_source` | PASS |
| Deployment-override subtractive semantics are unaffected by the locator generalization | `tests/test_platform_tools_integration.py` (`PINNED_ROLES` parametrization, unchanged), full predefined-role/deployment-override suite run green | PASS — see Documented Deviation 1 |
| Predefined-role discovery stays scoped to the platform-role tree | `tests/test_role_contract_suite.py`, `tests/test_role_governance_contract.py::test_importer_agent_extending_a_predefined_role_is_out_of_guard1_scope`, `::test_expected_role_surface_covers_exactly_the_pinned_roles` | PASS — see Documented Deviation 1 |
| Error and boot-failure messages name predefined roles by their new term | `tests/test_role_contract_suite.py::test_platform_role_contract_module_uses_predefined_terminology` | PASS |

### `agent-registration-serving`

| Requirement | Covering test(s) | Verdict |
|---|---|---|
| `create_app` accepts an explicit `{id: Agent}` registration | `tests/test_create_app_agents_param.py`, `tests/test_main.py::test_registration_of_a_predefined_role_matches_the_env_registration`, `::test_registration_serves_a_custom_folder_agent_on_both_channels` | PASS |
| Runtime ids are deployer-chosen, non-empty, and unique per registration | `tests/test_create_app_agents_param.py` (`_validate_runtime_id`), `tests/test_main.py::test_registration_rejects_an_invalid_id_before_any_runtime_is_built`, `::test_registration_accepts_an_arbitrary_id_without_parsing_it` | PASS |
| The `{deployment}__{role}` scheme and its sentinel are removed | `tests/test_agent_registrations_parsing.py`, `tests/test_main.py` (env-driven boot cases), `tests/test_no_duplicated_runtime_id_parsing.py` | PASS |
| `to_model_id`/`parse_model_id` and the inline `main.py` parser are deleted | `tests/test_openai_adapter.py::test_to_model_id_import_raises_import_error` (or equivalent), `tests/test_no_duplicated_runtime_id_parsing.py::test_no_module_parses_a_runtime_id`, `::test_the_scan_flags_every_move_of_the_removed_scheme`, `::test_the_scan_flags_a_renamed_successor` | PASS |
| `/v1/models` lists exactly the ids the operator named for the adapter | `tests/test_main.py` — `app.state.adapter_model_ids` assertions at lines 1994-1995, 2279, 2539, 2597, 2761 (adapter-exposed set stays distinct from the full runtime cache) | PASS |
| WhatsApp runtime binding resolves by registered id | `tests/test_main.py::test_registration_binds_whatsapp_and_publishes_only_adapter_ids`, `::test_registration_fails_boot_on_an_unmatched_channel_id` | PASS |
| WhatsApp binding still requires `untrusted_input=true` | `tests/test_main.py::test_registration_refuses_a_trusted_input_agent_on_whatsapp`, `::test_registration_binds_an_untrusted_input_agent_to_whatsapp` | PASS |
| WhatsApp binding still enforces the outbox lease timing budget | `tests/test_main.py::test_registration_enforces_the_lease_budget_for_a_custom_agent` | PASS |
| `DEPLOY_GRANTS` is keyed by the registered runtime id | `tests/test_main.py::test_registration_fails_boot_without_a_grant_for_the_id`, `::test_registration_falls_back_to_deploy_grants_keyed_by_the_id` | PASS |
| No implicit client-override root is assumed for a registered agent | `tests/test_main.py::test_registration_with_a_client_needs_an_explicit_root_config`, `::test_registration_rejects_clients_for_an_agent_valued_entry`, `::test_registration_rejects_clients_for_an_unregistered_id` | PASS |
| No default deployments path ships inside the installable package | `tests/test_demo_entrypoint.py::test_agents_system_demo_module_no_longer_exists`, `::test_examples_demo_app_exposes_build_app_and_main` | PASS |
| Boot fails loudly for every unresolvable registration, before serving begins | `tests/test_main.py::test_registration_one_unresolvable_entry_blocks_the_whole_boot` | PASS |

### `predefined-agent-governance` (Guard 1)

| Requirement | Covering test(s) | Verdict |
|---|---|---|
| A versioned, independent snapshot of every predefined role's surface exists | `tests/test_role_governance_contract.py::test_unmodified_role_matches_its_snapshot`, `::test_snapshot_does_not_grade_a_role_against_itself` | PASS |
| A contract check fails on any tools/permissions divergence lacking both a version bump and a CHANGELOG entry | `tests/test_role_governance_contract.py::test_divergence_with_neither_condition_fails_naming_both`, `::test_divergence_with_version_bump_but_no_changelog_still_fails`, `::test_divergence_with_changelog_but_no_version_bump_still_fails`, `::test_divergence_with_both_conditions_passes`, `::test_role_md_only_prose_edit_triggers_no_version_or_changelog_requirement` | PASS |
| The contract check names the exact divergence | `tests/test_role_governance_contract.py::test_removed_tools_and_permissions_are_also_named_on_divergence`, `::test_changelog_check_rejects_unrelated_substring_collision` | PASS |
| Guard 1 applies only to roles discovered under `platform_root/roles` | `tests/test_role_governance_contract.py::test_importer_agent_extending_a_predefined_role_is_out_of_guard1_scope`, `::test_expected_role_surface_covers_exactly_the_pinned_roles` | PASS |
| Guard 1's per-role version is independent of the package's own SemVer | none found | **GAP (non-blocking, see below)** |
| `CHANGELOG.md` exists and is the recorded location for Guard 1 entries | Direct read: `CHANGELOG.md` exists at the repo root (`[0.2.0]` section present); `release-please-config.json:22` `"changelog-path": "CHANGELOG.md"` points at it | PASS |

**Verdict counts: 36 PASS, 0 FAIL, 1 GAP (test coverage only, not a behavioral failure).**

## Documented Deviations (known, non-blocking)

1. **Four requirements covered by behavior, not cited by title in `tasks.md`.** PR7-T3's own consistency check (recorded in `tasks.md`'s Follow-ups section) already found that "The base prompt contract is appended last...", "`untrusted_input` ⊥ T3-tier permission exclusion...", "Capability tiers and the T3 sandbox gate apply...", and "Deployment-override subtractive semantics are unaffected..." are each exercised by an existing test (confirmed directly above), but no task's `Satisfies:` line quotes their exact requirement title verbatim. This verify pass independently confirms the same conclusion by reading the actual test bodies: a citation gap in `tasks.md`, not a coverage gap in the test suite. No code or test change is needed; a future docs-only pass may amend the `Satisfies:` lines for precision.
2. **"Predefined-role discovery stays scoped to the platform-role tree" is proven negatively, not by a dedicated positive-discovery test.** Coverage is `test_role_governance_contract.py`'s Guard-1 scoping tests (an importer agent extending a predefined role is never subject to Guard 1) plus the full existing `PINNED_ROLES`/`discover_platform_roles()` regression suite passing unchanged. This is the same discovery mechanism the spec requires be reused, exercised indirectly rather than through a test asserting discovery's return set size directly against an adjacent importer folder. Judged sufficient: the two scenarios the requirement's own spec text describes (adjacent-folder non-discovery, unchanged discovery count) are both properties of the unmodified `discover_platform_roles()` function, which no PR in this chain touches.

## Open Follow-ups (not part of this change; noted for future tracking)

- **Guard 1 per-role SemVer independence has no dedicated regression test (genuine gap).** `design.md`'s D8 narratively separates a predefined role's own `version:` frontmatter (Guard 1's MAJOR-bump requirement) from the installable package's own release version (bumped MINOR by `release-please`'s `bump-minor-pre-major: true` under a `BREAKING CHANGE:` footer), and the shipped code never conflates the two — `tests/test_role_governance_contract.py` reads a role's own `version:` field, never `agents_system.__version__` or `pyproject.toml`. But no test in `tests/test_role_governance_contract.py` or `tests/test_check_role_governance_marker.py` exercises the two-spaces-are-independent claim directly (confirmed: no `SemVer`/`pyproject`/`__version__` reference in either file). This is a coverage gap, already identified verbatim by PR7-T3's own consistency check and carried into `tasks.md`'s Follow-ups section — not newly discovered by this verify pass. It does not indicate broken behavior: nothing in the shipped implementation reads or compares the package's own version anywhere near Guard 1's check. Recommend filing a GitHub issue for a regression test proving a role `version:` MAJOR bump does not require, and a package MINOR release does not substitute for, a `BREAKING CHANGE:` marker on `platform/roles/**/manifest.md`, or vice versa — matching this repo's existing pattern of tracking a scoped, non-blocking gap as a follow-up issue (e.g. issue #93 for PR2's review-fix follow-ups, since closed).
- **`openspec/specs/permission-hierarchy/spec.md`'s promotion note.** `tasks.md`'s own Follow-ups section already records that this file's presence under `openspec/specs/` (rather than only inside the archived `permission-model` folder) was a pre-existing process gap, not something this change's tasks created or need to fix. Unaffected by this archive pass.

## Summary

Every requirement in all three delta specs (`agent-definition-locator`, `agent-registration-serving`, `predefined-agent-governance`) has an identifiable covering test on `origin/main`, independently confirmed by reading the actual test files rather than trusting `tasks.md`'s citations alone — 36 of 37 requirements PASS outright; one (Guard 1's per-role SemVer independence) is a documented, already-tracked test-coverage gap with no corresponding behavioral defect, matching the same non-blocking-follow-up pattern the `permission-model` verify pass used for its own single open item. Full suite (2290 tests), `ruff check`, `ruff format --check`, and `mypy src/` are all clean.

**No FAIL findings. Proceeding to archive.**
