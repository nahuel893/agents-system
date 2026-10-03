# ADR-010 — Capability-based evals, with predefined roles as reference agents

**Status:** Accepted (2026-10-03) · **Date:** 2026-10-03 · **Amends:** ADR-002 D, D.17, E.18; ADR-004 D3, D7, D8 · **Issue:** #219

**Acceptance.** The owner accepted this ADR on 2026-10-03, and every open question below is resolved as its recommendation states.

## Summary

The live suite tests the kit through the eight concrete predefined roles, and those same roles ship in the core as supported API. This ADR separates the two jobs. Evals are organized by **capability**, in two layers: a deterministic **mechanism** layer that runs offline with a scripted model that always attempts the guarded path, and a **behavior** layer that runs each capability against a real model on a minimal fixture agent. A few **composition** scenarios on two or three reference agents sit on top. The core keeps only the generic parents, `base` and `agent`. The eight concrete roles become **reference agents**: examples and test subjects that live in the repository, not supported API. A change runs the live scenarios its type calls for, and the capability suite becomes a kit feature: a per-model capability matrix that an adopter runs to compare candidate models.

## Context

**One set of roles, two jobs.** Today the concrete roles are product and test subject at once:

- **Product.** `platform/` is force-included in the wheel as `agents_system/platform` (`pyproject.toml:97-98`). The tree is `base` (abstract, `platform/roles/base/role.md:4`) ← `agent` (`platform/roles/agent/manifest.md:4`) ← seven roles, with `developer-agent` extending `operator-agent` (`platform/roles/developer-agent/manifest.md:4`). Adopters can extend them by name (`Agent(extends="sales-agent")`, `docs/platform/features.md:21`) and serve them by name (`AGENT_REGISTRATIONS`, parsed by `_parse_agent_registration`, `src/agents_system/main.py:943`). Guard 1 (ADR-004 D8) treats their tool and permission surface as a breaking-change boundary: `EXPECTED_ROLE_SURFACE` (`tests/platform_role_contract.py:216`) and the `predefined-role-governance-marker` job (`.github/workflows/ci.yml:687`). So every prompt or policy change to one of them is a change the project must support.
- **Test subject.** Each of the 26 scenario files in `evals/scenarios/` names a concrete role (`Scenario.role: str`, `src/agents_system/evals/schema.py:121`), and so do the five guardrail scenarios in `evals/scenarios/guardrails/`, Phase 1 (`tests/test_live_eval_phase1.py`), the smoke test (`tests/test_live_smoke.py:123`) and Phase 4, whose custom agent `restock-helper` extends `platform/roles/sales-agent` (`tests/fixtures/agents/phase4_lifecycle/roles/restock-helper/manifest.md:3`).

**What the v0.5.1 run showed.** The full live suite on 2026-10-03 (`scripts/run-live-suite.sh`, `openai/gpt-6-sol`, `EVAL_RUNS=3`):

- Smoke, sql-tool, phase4, load-fake and load-real passed. Roles, guardrails and phase1 failed. No guard was ever broken.
- The roles module alone took 80 turns, 64% of the input tokens and about 11 of 21 minutes.
- `support_agent_boundary` and `05_max_tool_calls` were never exercised: the model never attempted the guarded path, so the #81 gate failed them with "never exercised" (the same class as #218). A careful model that declines the forbidden action leaves the mechanism untested.
- `support_agent_happy` failed 0/3 because the model escalated a simple question. That is a property of one role's prompt, not of the kit. `summary_agent_escalation` held 2/3 against a 100% bar, and `sales-agent.order_writer` passed 2/3 against 80%.

The failures say little about the kit, and most of the cost goes to tuning eight prompts the owner does not want to support.

**How adopters build agents.** An adopter's agent is an `Agent(...)`. Its default parent is `"platform/roles/agent"` (`src/agents_system/agent/spec.py:273`), and `agent` extends `base`. The base prompt contract does not come from a role file: `resolve()` appends the code constant `_BASE_PROMPT_CONTRACT` (`src/agents_system/harness/loader.py:1186`) through `_append_base_contract` (`loader.py:1200`, called at `loader.py:3820` and `loader.py:3857`), once, after every fold. The same `resolve()` runs the `untrusted_input`⊥T3 check. `tests/test_agent_from_params.py:51` pins both for an inline `Agent(extends="agent")`: the prompt ends with the contract's last clause, and an `untrusted_input` agent asking for `exec:command` raises `UntrustedInputGrantError`. Nothing an adopter relies on comes from a concrete role unless the adopter names one in `extends`.

**The predefined roles use a privilege adopters do not have.** A platform role may loosen its parent's autonomy and limits and reword the escalation conditions it inherits, because the same author writes both files. An importer-authored definition is held to its parent's effective values by `_apply_importer_ceiling` (`loader.py:3395`). Resolving copies of the eight folders as importer folders (`FolderLocator`) on `main` shows that five would fail. `data-agent` and `summary-agent` declare `autonomy: full` over `agent`'s `supervised` (`platform/roles/data-agent/policy.md:4`, `platform/roles/summary-agent/policy.md:4`). `developer-agent` sets `total_execution_timeout_s: 120` over `operator-agent`'s 30 (`platform/roles/developer-agent/policy.md:15`). `data-agent`, `summary-agent`, `sales-agent` and `orchestrator` reword an inherited escalation condition, which `_validate_escalation_wording` (`loader.py:3316`) refuses. `accountant-agent`, `operator-agent` and `support-agent` resolve unchanged. So the live suite tests agents that resolve through a path no adopter's agent can take.

**What exists to build on.**

- #156 already moved one deterministic fact out of the live suite: Layer-1 exclusion is proven offline by `test_boundary_scenario_permission_grant_excludes_target_tool` (`tests/test_platform_tools_integration.py:376`).
- Scripted tool-calling models exist: `ToolAwareFakeModel` (`tests/test_agent_runtime.py:29`, `tests/test_eval_runner.py:40`) drives `run_scenario` offline, and `test_max_tool_calls_breach_terminates_gracefully` (`tests/test_agent_runtime.py:536`) proves the limit with a model that always asks for a tool.
- `create_app(chat_model=...)` (#83) injects a model into the served app. Its current fake, `_LoadTestFakeModel` (`tests/test_live_load_phase3.py:70`), answers without calling any tool, so it proves the seam, not a guarded path.
- `boot_live_harness` already accepts an `Agent` (`role: Agent | str`, `src/agents_system/evals/live_harness.py:921`). `run_scenario` does not: it resolves `scenario.role` as a predefined-role name (`src/agents_system/evals/runner.py:918-919`).

**Numbering.** ADR-006 is reserved for delegation (#170), and ADR-008 (#159) and ADR-009 (#189) are proposed in open pull requests. This is ADR-010.

## Decision

### 1. Capability-based evals, in two layers

Evals are organized by what the kit promises, not by which agent shows it. Each capability has one owner layer:

| Capability | Layer | What a passing scenario proves |
|---|---|---|
| Call-time interceptor | Mechanism | A sensitive call without the grant is blocked, audited and never executed (`tool_blocked`, `audit_event`, `not_executed`) |
| Execution limits | Mechanism | `max_tool_calls`, `tool_call_timeout_s` and `total_execution_timeout_s` end the turn (`limit_reached`) |
| Audit | Mechanism | Every attempt, block and denial reaches the sink, redacted |
| Sandbox | Mechanism | `read_file` and `use_term` outside the sandbox root never execute (needs `bwrap`) |
| `untrusted_input`⊥T3 | Mechanism | Already offline (`tests/test_untrusted_input_invariant.py`); unchanged |
| Tool choice and arguments | Behavior | The model picks the right tool with valid arguments |
| Commands and output | Behavior | The model runs a command and acts on what it printed |
| Tool-error recovery | Behavior | A failing or refused tool is reported, never papered over |
| Grounding | Behavior | Answers carry only what tools returned |
| Escalation | Behavior | Escalates when a policy condition holds, and not on a simple question |
| Multi-turn memory | Behavior | Uses facts from earlier turns |
| Untrusted-input resistance | Behavior | Ignores instructions planted in a tool result |

- **Mechanism (deterministic, every PR).** A scripted model that always emits the guarded call, in the shape of `ToolAwareFakeModel`, runs through the same `run_scenario` and `evaluate_assertions` the live suite uses, so one assertion vocabulary describes both layers. Results are hard pass/fail. "Never exercised" cannot happen, because the script always attempts the path. Once an offline equivalent lands, the live mechanism scenarios (`02_layer2_revalidation`, `04_t3_containment_read_file`, `05_max_tool_calls`, `06_tool_call_timeout` and the seven `*_boundary` files) leave the live gate.
- **Behavior (real model).** Each capability gets one minimal fixture agent: it extends `agent`, equips one to three tools on the reference backends (ADR-002 C.15), and its prompt says only what the capability needs. What is under test is the kit's generic prompt, the base contract and the tool descriptions. The #81 gates stay as they are: 80% for happy-path scenarios, and 100% of exercised runs for guardrail scenarios, with at least one exercised. Every scenario states a complete request that a careful model would act on (#218).
- **Composition (real model, few).** Atomic tests miss a model confused by many tools and a long prompt, so two or three reference agents keep a small set of end-to-end scenarios through `boot_live_harness` (the HTTP API and the webhook), in the Phase 1 shape. This is the top of the test pyramid, not its body.

The capability scenarios and their fixture agents ship inside the package (`agents_system.evals`), because part 4 runs them on an adopter's machine. Composition scenarios stay in the repository.

### 2. Predefined roles become reference agents

- **The core keeps `base` and `agent`.** They stay under `platform/roles/`, keep shipping in the wheel, and `Agent`'s default parent does not change.
- **The eight concrete roles become importer folders** under `examples/agents/<name>/` (open question 1), resolved with `Agent.from_folder` like any adopter's agent. `developer-agent` keeps its parent through a sibling reference, `extends: ../operator-agent` (ADR-004 D2 allows it inside one importer root). The five that fail `_apply_importer_ceiling` today are brought within it in the same change (open question 2), so the reference agents exercise the path adopters use.
- **They are examples, not supported API.** The docs present them as starting points to copy. They carry no SemVer promise and leave Guard 1. A change to one of them is governed by part 3.
- **Guard 1 narrows to the generic parents.** It exists because a predefined role's surface is a baseline other agents build on (ADR-004 D8). After the move, the only such baseline is `agent`: `EXPECTED_ROLE_SURFACE` and `PINNED_ROLES` (`tests/platform_role_contract.py:206`) keep only `agent`, and the marker script's prefix `platform/roles/` (`scripts/check_role_governance_marker.py:25-26`) keeps matching, now only the two parents. The required status check stays. ADR-009 (#189) proposes version governance for every agent; if accepted, reference agents fall under it like any adopter's agent. This ADR does not depend on it.
- **The contract suite walks parents and reference agents.** `tests/test_role_contract_suite.py` walks `discover_concrete_platform_roles()` (`tests/platform_role_contract.py:124`). It keeps doing so for `agent`, and a second walk covers the reference agents as fixtures, as do `test_every_platform_role_boots_end_to_end` (`tests/test_platform_tools_integration.py:81`), `test_every_concrete_platform_role_explicitly_declares_untrusted_input` (`tests/test_untrusted_input_invariant.py:306`) and `test_no_role_outside_the_operator_branch_can_reach_the_host` (`tests/test_role_resolution_pinned.py:359`).

Every place that depends on the concrete roles today, and what happens to it:

| Dependency | Where | After this ADR |
|---|---|---|
| Wheel contents | `pyproject.toml:97-98` | Ships `base` and `agent` only |
| `extends: <role>` by name | `_extends_target` (`src/agents_system/harness/loader.py:1533`) | Deprecated, then a `DefinitionError` naming the example to copy (see Migration) |
| Env-only serving | `_parse_agent_registration` (`main.py:943`); `deploy/systemd/agents-system.env.example:89` | Names only `agent` unless open question 4 adds a folder value |
| Client overrides | `resolve(..., client=)` accepts only a predefined-role name (`loader.py:3787`) | Keep working for `agent`; an adopter edits its own copied folder instead |
| Built-in spawn permissions | `spawn:sales-agent`, `spawn:data-agent`, `spawn:summary-agent` (`src/agents_system/permissions/builtins.py:148-150`) | Stay registered until ADR-006 (#170) decides delegation |
| Live harness default | `role="sales-agent"` and `SALES_AGENT_PERMISSIONS` (`evals/live_harness.py:921`, `:995`, `:148`) | Takes a reference agent as an `Agent`, with an explicit grant |
| Eval runner | `Scenario.role: str` (`evals/schema.py:121`); `resolve(scenario.role, ...)` (`evals/runner.py:918-919`) | Also accepts a folder or an `Agent` |
| Scenarios | 26 files in `evals/scenarios/`, 5 in `evals/scenarios/guardrails/` | Split into capability scenarios and reference-agent composition scenarios |
| Live tests | `tests/test_live_eval_{roles,sales_agent,guardrails,phase1,phase4}.py`, `tests/test_live_smoke.py`, `tests/test_live_load_phase3.py` | Repointed; `restock-helper` extends the reference `sales-agent` through `Agent.from_folder(..., extends=...)` |
| Guard 1 | `tests/platform_role_contract.py`, `tests/test_role_governance_contract.py`, `scripts/check_role_governance_marker.py`, `.github/workflows/ci.yml:687`, `openspec/specs/predefined-agent-governance/` | Narrowed to `agent` (above) |
| Other offline tests | 69 files under `tests/` name a concrete role; the most hits are in `test_main.py`, `test_role_resolution_pinned.py`, `test_loader_fail_closed.py`, `test_openai_adapter.py` and `test_harness_factory.py` | Use `agent`, an inline `Agent`, or a reference agent as a fixture |
| Connector rationale | `src/agents_system/connectors/order_connector.py:9`, `report_connector.py:54` explain that the tool always exists because a role names it | Behavior unchanged; the comments name the reference agent |
| Public docstring | `src/agents_system/__init__.py:68` (`extends="sales-agent"`) | Shows `extends="agent"` |
| Demo | `examples/demo/app.py` serves through `AGENT_REGISTRATIONS`; `docs/platform/demo-entrypoint.md` registers `sales-agent` | Registers reference agents with `create_app(agents=...)` |
| Docs | 43 files under `docs/` (EN and ES), led by `features.md:21`, `role.md`, `library-usage.md`, `deployment.md`, `live-eval.md`, `demo-entrypoint.md`; the README's library-first and Guard 1 sections | Rewritten in the same changes, EN + ES |
| Specs | `openspec/specs/{agent-definition-locator,agent-registration-serving,permission-hierarchy,predefined-agent-governance}/` | Amended; the term "predefined role" retires |

### 3. Which live scenarios a change runs

A runtime change runs the capability scenarios. A change to a reference agent runs that agent and every agent that extends it, read from the `extends:` graph. This table is proposed to replace the Definition of done table in `AGENTS.md` once this ADR is accepted; this PR does not edit `AGENTS.md`.

| You changed | Run at least |
|---|---|
| The runtime, the graph, the interceptor, the permission model, limits, audit or the sandbox | The mechanism suite (part of `pytest -q`) and every behavior capability scenario |
| `base`, `agent` or the base prompt contract | Every behavior capability scenario and every composition scenario |
| A tool's description or schema, or a connector | The capability scenarios that equip it, and the composition scenarios of every reference agent that declares it |
| A reference agent's prompt, policy or manifest | That agent's composition scenarios and those of every agent that extends it (`operator-agent` → `developer-agent`) |
| A channel, the served app or conversation memory | The composition scenarios through the harness, and `tests/test_live_smoke.py` |
| The `Agent` API, `extends`, skills or the importer | `tests/test_live_eval_phase4.py` |
| Concurrency, the webhook worker, the outbox or connection pools | `tests/test_live_load_phase3.py` |
| The SQL tool or parameterized reports | `tests/test_live_eval_sql_query.py` |
| The eval runner or the capability suite | The capability scenarios on the reference model |

The reference model, the rule to force it and the release gate (`scripts/run-live-suite.sh`, now grouped as mechanism, behavior, composition) do not change.

### 4. A per-model capability matrix

The capability suite is a kit feature. Given candidate models and the capabilities an adopter's agents need (all by default), it runs each capability scenario `EVAL_RUNS` times per model and reports a model × capability table: success rate against the gate, exercised runs, tokens, cost and duration. Every one of those numbers is already in `ScenarioResult` (`evals/runner.py:193`), and `write_results` already writes JSON and Markdown to the gitignored `evals/results/`. The entry point is a Python function in `agents_system.evals` with a thin script around it, behind the extras the eval features already need (`postgres` and the provider extra, `docs/platform/features.md`). An adopter may add their own scenarios on their own agents to the same matrix once the runner accepts an `Agent`. The docs state what a cell means: a rate for one model on the kit's generic prompt and a minimal fixture, not a verdict on the adopter's own agent.

## Migration

1. **Release N (the next minor; `bump-minor-pre-major` is on, `release-please-config.json:3`).** The reference agents land in `examples/agents/`, already within the importer ceiling. The eight names still resolve from `platform/roles/`, and resolving one emits a `DeprecationWarning` that names the example folder to copy.
2. **Release N+1 (breaking, `feat!`).** The eight folders leave `platform/roles/`. `extends: sales-agent` and an `AGENT_REGISTRATIONS` value naming a concrete role fail at load or boot with a `DefinitionError` naming the example folder, as ADR-004 D2 fails loudly today.
3. **What an adopter does.** Copy `examples/agents/<name>/` into the project and load it with `Agent.from_folder(path)`. Replace `extends="sales-agent"` with the copy (a sibling `extends:` inside the same importer root, or `extends=Agent.from_folder(...)` in Python). Move a client override of a concrete role into edits on the copy, since deployment overrides apply to platform roles only (ADR-004 D5). Register the copy with `create_app(agents=...)`. Permission wire names and `DEPLOY_GRANTS` do not change. An adopter who copied one of the five adjusted agents gets the adjusted policy, which the release notes list.

## Consequences

- Live runs get cheaper and their failures become legible: a capability failure points at the kit, a composition failure at one example. The roles module that took 64% of the input tokens shrinks to a few composition scenarios.
- Mechanisms are proven on every PR with a hard pass/fail, and a careful model can no longer leave a guard untested.
- The reference agents resolve through the importer path, so the live suite tests what adopters run, including `_apply_importer_ceiling`.
- Breaking for anyone who extends or serves a concrete role by name, after one deprecation release. `pip install` users find the examples in the repository, not in the wheel.
- Five reference agents change policy to fit the importer ceiling, and their scenarios are re-baselined.
- `deployments/<client>/<role>/` overrides keep only `agent` as a subject, and the D.17 contract suite covers one platform role instead of nine.
- ADR-002 E.18 measures per capability and per model instead of per role and per model; ADR-002 D's tree keeps only `base` and `agent`; ADR-004 D3's `extends` by predefined-role name, D7's demo registration and D8's Guard 1 scope change as above. Each gets a pointer on acceptance, as ADR-004 did for ADR-002.

## Open questions for the owner

1. **Where the reference agents live.** `examples/agents/` is visible, documented and outside the wheel; `tests/fixtures/` is outside the wheel too but reads as internal, so nobody finds it as an example; keeping them in the package keeps them de facto supported. **Recommendation:** `examples/agents/`. The live suite runs from a checkout, so being outside the wheel costs it nothing.
2. **How the five failing agents fit the importer ceiling.** Either adjust them (`data-agent` and `summary-agent` become `supervised`; the four that reword an inherited condition move that wording into new, role-specific conditions; `developer-agent` drops its 120 s override and inherits 30 s, with `operator-agent` raised to the platform default of 60 s, `loader.py:171`, if its scenarios need more; 120 s exceeds that default too), or give the reference agents a privileged root of their own. **Recommendation:** adjust them. A privileged root would test a path adopters cannot use.
3. **Deprecation window.** **Recommendation:** one minor release with a `DeprecationWarning`, then removal in the next breaking release.
4. **Env-only serving.** Without a change, `AGENT_REGISTRATIONS` can serve only `agent`. **Recommendation:** accept a folder value resolved under a configured agents root, with the same containment as `FolderLocator`. Otherwise env-only deployments must move to `create_app(agents=...)`.
5. **Which reference agents carry composition scenarios.** **Recommendation:** `sales-agent` (untrusted input, writes, the webhook), `developer-agent` with `operator-agent` (the sandbox, a two-level chain) and `data-agent` (reports). The other five keep offline contract tests only, no live scenarios.
6. **Publishing the matrix.** **Recommendation:** each release PR carries the matrix for the reference model and the two second-opinion models (`docs/platform/live-eval.md`, "Reference model"), in the PR body, not committed.

## Follow-ups

Issues to file once this ADR is accepted (none is filed now):

1. Mechanism suite offline: a scripted model for every guardrail and `*_boundary` scenario, then their removal from the live gate.
2. Eval runner: `Scenario` and `run_scenario` accept a folder or an `Agent`, not only a predefined-role name.
3. Capability fixture agents and behavior scenarios, one per capability, shipped in `agents_system.evals`.
4. Reference agents in `examples/agents/`, fitted to the importer ceiling, with the deprecation warning (release N).
5. Removal from `platform/roles/` (release N+1): Guard 1 narrowed, the contract suite and live tests repointed, the live harness default replaced.
6. Served app and demo: the demo registers reference agents; the `AGENT_REGISTRATIONS` folder value if open question 4 is accepted; the env examples updated.
7. Docs and specs listed in part 2, EN + ES, and the pointers in ADR-002 and ADR-004.
8. The Definition of done table in `AGENTS.md`, the suite grouping in `scripts/run-live-suite.sh`, and a helper that lists the agents extending a reference agent.
9. The capability matrix: API, script and docs.
