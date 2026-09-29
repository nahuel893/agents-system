# ADR-009 — Agent versioning and a development lifecycle per agent

**Status:** Proposed · **Date:** 2026-09-29 · **Issue:** #189 · **Would amend:** ADR-004 D8 (Guard 1 scope)

## Summary

Every agent already has a `version`, and nothing reads it. This ADR proposes to make it mean something, in five decisions that stand on each other. (1) A **fingerprint**, a content hash over the *effective* definition (the resolved definition plus the skill contents it loads), is recorded next to the version in a reviewed lock file; CI compares it with the lock on the base branch, and a fingerprint that moved without a version bump fails the build, and a change to the agent's authority (tools, permissions, autonomy, delegation, `untrusted_input`) requires a MAJOR bump. This is Guard 1 (ADR-004 D8) extended from the predefined roles to every agent. (2) **Evals are the release gate:** each agent carries its own scenarios, and a version is released only when the #81 gates pass for that exact fingerprint and that exact set of scenarios. (3) **Traceability:** every turn, audit event and metric names the agent and its version, so two versions can be compared with data. (4) **Deploy and rollback:** a served deployment pins the version and the fingerprint it expects and refuses to boot on either mismatch, the same fail-loud stance as `DEPLOY_GRANTS`; rollback restores the previous revision and both pins, and the fingerprint proves it is the same definition. (5) **Lifecycle states and tooling:** draft, evaluated, released, deprecated, driven by `agents-system agent check | eval | release | diff`. Nothing here is implemented; the owner decides, and implementation issues follow acceptance.

## Context

**What exists.** `Agent.version` (`src/agents_system/agent/spec.py`) defaults to `"1.0"`. The loader reads it from `role.md` frontmatter, then `manifest.md`, then falls back to `"1.0"` (`_load_role_files` and its deployment twin in `harness/loader.py`), as a plain `str`: nothing validates its shape. It is copied into `RawDefinition` and `AgentDefinition`. In an `extends:` chain the child's own value wins (`version=child.version` in `_fold_parent_into_child`), so a parent's version and a parent's change never reach a child.

**Why it is decorative.** Outside the loader, nothing in `src/` reads `AgentDefinition.version`, so:

- nothing checks that it changes when the agent changes: a prompt, a tool or a model can change with the version untouched;
- metrics are labeled by `runtime_id` only (`observability/metrics.py`: `agent_turns`, `agent_turn_duration_seconds`, `agent_tokens`, `agent_cost_usd`), and `agent_tool_calls` by `tool`;
- audit events carry `role` and `deployment` and an `actor` (`audit/events.py::_AuditEventBase`, filled by `_extract_role_deployment`), never a version, although the definition passed to that function has one;
- the `runtime.turn_usage` log line carries no version either.

**What does enforce a change today.** Guard 1 (ADR-004 D8) is two halves, both for predefined roles only. A pytest half (`tests/platform_role_contract.py::check_role_governance_snapshot`) compares each role's resolved `tools` and `permissions` with a hand-maintained `EXPECTED_ROLE_SURFACE` and fails on a divergence unless the role's MAJOR component grew and `CHANGELOG.md` names the role. A CI half (`scripts/check_role_governance_marker.py`) requires a conventional-commit breaking marker on a PR that touches `platform/roles/**/manifest.md`. ADR-004 D8 says explicitly that an importer-authored `Agent` "can never become a governance subject through this mechanism", because it has no name in `PINNED_ROLES`. The prompt, the model, the policy and the limits are outside Guard 1 even for predefined roles.

**What the harness already gives us.**

- *Registration and pinning surface.* `create_app(agents=...)` and `AGENT_REGISTRATIONS` map a deployer-chosen opaque id to an `Agent` or to `"role"` / `"role@client"` (ADR-004 D5). The id is the natural `agent_id`; this ADR introduces no second identity.
- *Explicit, fail-loud deployment choices.* `DEPLOY_GRANTS` needs an entry per model id or boot fails (ADR-003). A version pin is the same kind of decision.
- *Evals with gates.* `evals/scenarios/*.yaml` are gated by #81: a guardrail scenario passes only at 100% of *exercised* runs and at least once exercised, a happy-path scenario at 80% unless it declares a threshold and a reason (`docs/platform/live-eval.md`, "Gating"). The reports carry the model, tokens and cost per scenario. Live runs are opt-in and deliberately not a per-PR gate.
- *Prompt outside the checkpoint.* `_call_model` prepends the system prompt at call time and never stores it in state (`agent/graph.py`). A resumed thread therefore runs under whatever prompt is deployed *now*; the thread stores messages, not the agent.

**What does not exist.** There is no CLI (`pyproject.toml` declares no console script). The eval runner resolves `scenario.role` through the platform role tree, with an optional deployment `client` (`evals/runner.py`): it cannot target an importer `Agent` today. There is no durable checkpointer (#24) and no agent-own long-term memory (ADR-002 A.3, G.25).

**Numbering.** ADR-004 is library-first agents, ADR-005 the operational-safety roadmap, ADR-006 is tentatively reserved for delegation, ADR-007 the read-only SQL tool and ADR-008 the MCP proposal. This is ADR-009.

## Decision

### D1 — Version plus fingerprint, checked for every agent

**The fingerprint.** SHA-256 over a canonical serialization (sorted keys, tuples as lists, UTF-8) of the *effective* definition: the resolved `AgentDefinition` (after the `extends:` fold and after the platform appends its base-contract clauses) **plus the skill contents loaded for it**. The resolved definition alone is not enough. It carries skill *names* and the places they are read from (`skills`, `skills_folder`, `inline_skills`, `importer_root`), not the text of a folder or deployment skill, and its `system_prompt` is the role body plus the base contract, not what the model receives. The text the model receives is composed later, at equip time, from that body, the skill files, the escalation block and the base contract (`harness/factory.py::_compose_prompt`, sent by `_call_model` as `EquippedRuntime.system_prompt`). So the fingerprint is computed after the same skill loading (`_load_skills`, same source precedence, including the deployment's `skills/` folder when a client is given), and the prompt section hashes the composed prompt. `agent check` therefore runs that loader offline; a check that hashed only the `AgentDefinition` would not move when a skill file is edited. The fingerprint is computed per section, and the agent's fingerprint is the hash of the section digests:

| Section | Fields | Bump it requires |
|---|---|---|
| **authority** | `tools`, `permissions`, `command_tools`, `untrusted_input`, `autonomy`, `delegation_policy` | **MAJOR** |
| **prompt** | the composed `system_prompt` (role body, loaded skill contents in order, escalation block, base contract), `context` | MINOR |
| **model** | `model` (`provider`, `model`, `temperature`, `max_tokens`; never a credential, see `_validate_model_config`) | MINOR |
| **policy** | `escalation_rules`, `memory_policy`, `audit_policy` | MINOR |
| **limits** | `execution_limits` | MINOR |

Excluded on purpose: the `version` itself, the file layout, comments and whitespace outside the prompt, the machine-specific `skills_folder` and `importer_root` paths (what counts is the skill *contents*, which enter through the loaded skills), `role_name` and `deployment` (identity and provenance: what a deployment override changes reaches the sections above through the fields it narrows and the skills it supplies), and the deployment's own configuration (`DEPLOY_GRANTS`, the registration id, the checkpointer). Grants are deployment-owned and already explicit (ADR-003); putting them in the agent's fingerprint would make one agent's version depend on where it is deployed.

`model=None`, the common case, means "use the global provider default" (`ADAPTER_PROVIDER`). That default is not part of the definition, so changing it is outside the fingerprint. This is deliberate: it is a deployment choice like `DEPLOY_GRANTS`, not an edit of the agent. It is not invisible: the model is recorded in the evaluation evidence (D2) and on the traceability fields (D3). An agent whose behavior must not follow the default should declare its own `model`, which is then in the *model* section.

Hashing the effective definition instead of the files is the point. A change to a parent role or to the platform base contract reaches a child through `extends:` while the child's own `version:` (which does not inherit) stays put; a hash of the child's own files misses that, and picks up noise the definition does not contain. A skill file edit is caught for a different reason: skill files are read from disk at equip time, not through `extends:`, so it moves the fingerprint only because the loaded skill contents are part of its input. The price is that a platform release that edits the base contract moves the *prompt* section of every agent. That is a MINOR requirement for each of them, and it is the honest signal: their behavior can change.

**The reviewed record.** A committed lock file next to the agent's definition (`agent.lock.json`; for an agent built inline in Python, the path its owner passes to the CLI) stores `version`, `fingerprint`, the section digests, the sorted `tools` and `permissions` (so a `diff` can name what changed), the lifecycle state (D5) and the last evaluation (D2). It plays the role `EXPECTED_ROLE_SURFACE` plays for predefined roles: an independent snapshot that the definition cannot update by itself. Regenerating it is a deliberate command, and it appears in the same diff as the version bump a reviewer is looking at.

**The check** (`agents-system agent check`, and a pytest helper with the same logic for teams that gate in their own suite) re-resolves the agent offline (loading its skills, as above) and applies two comparisons.

- **Consistency.** The working lock must equal what the working definition produces (fingerprint, section digests, scenario digest). Otherwise the lock is stale and the check fails: regenerate it.
- **History.** The working lock is compared with the lock on the PR's base branch (in CI, `git show <base>:<path>/agent.lock.json`, or the lock at the last release tag): a prior record the PR cannot rewrite. A lock regenerated in the same PR as the change proves only that the PR agrees with itself, so the version rules below are applied across that boundary, never against the file the PR just wrote. An agent with no lock on the base is new and passes as a draft. Where no base is available (a local run, an inline agent with no history), only consistency is checked and the output says that the history rule was not enforced.

The history comparison applies to importer agents and predefined roles alike; it is the independent baseline that importer agents lacked under Guard 1.

| Working lock vs base lock | Result |
|---|---|
| Same fingerprint | Pass |
| Fingerprint changed, version unchanged | **Fail:** "changed without a version bump", naming the sections that moved |
| Authority section changed, MAJOR not increased | **Fail:** MAJOR required, naming the tools/permissions added or removed |
| Any other section changed, version not increased | **Fail** |
| Version lower than the lock's | **Fail** |
| Version increased, fingerprint unchanged | Pass with a note (a re-cut release) |

Version shape becomes `MAJOR.MINOR` (digits only), which is what Guard 1 already parses; today anything a manifest says is accepted, so validating the shape is a small behavior change (see Open questions).

**Guard 1 and predefined roles.** If accepted, a predefined role gets a lock like any other agent, and the hand-written `EXPECTED_ROLE_SURFACE` becomes redundant. The commit-marker CI job and its `CHANGELOG.md` half stay: they serve the *consumers* of a predefined role, who learn of a breaking change through the release notes, which a fingerprint cannot write. Retiring the snapshot is a follow-up, not part of this decision.

### D2 — Evals are the release gate

- **Where scenarios live.** An agent's scenarios sit in an `evals/` folder inside its own folder, in the existing YAML schema, and target that agent without a `role:` line. The repository-level `evals/scenarios/` keeps serving the predefined roles. This needs the runner to accept an importer folder or an `Agent`, which it cannot today.
- **What passes.** The #81 gates, unchanged: 100% of exercised runs for guardrail scenarios, 80% (or a declared threshold with its reason) for happy-path ones. No new threshold is introduced here.
- **Recommended floor.** A version with no scenarios cannot be released, and an agent that holds any T2 or T3 tool (ADR-003) must carry at least one guardrail scenario. Without that, "evaluated" would be satisfied by an agent that was never asked to misbehave.
- **The evidence is bound to the fingerprint and to the scenarios.** The *scenario digest* is a SHA-256 over the complete inventory of the agent's scenario files (sorted paths and canonical contents, so adding, removing or editing a scenario, its assertions or its declared threshold moves it) plus the gate parameters in effect (the #81 guardrail and happy-path thresholds and the number of runs). `agent eval` runs the scenarios against the current definition and writes the result into the lock: fingerprint, scenario digest, model, per-scenario gate verdicts, date. `agent release` refuses unless the recorded evaluation matches *both the current fingerprint and the current scenario digest*, and the evaluation is repeated after either changes. Adding a failing guardrail scenario without touching the agent leaves the fingerprint alone but moves the digest, so the old passing result no longer counts. The offline check recomputes the digest at no cost, so a lock that claims `released` over scenarios that have since changed is reported as draft. This is what makes "evaluated" mean something.
- **Where it runs.** Live evaluation costs money and needs a provider key, and #81 keeps it out of the per-PR path on purpose. So the gate splits: the offline fingerprint check (D1) is free and runs on every PR; the live evaluation runs at release time, by the agent's owner or a scheduled job, on the reference model the live-eval doc names. Which model the gate must use when the agent declares its own is an open question.

### D3 — Traceability: `agent_id`, version and fingerprint on everything

`agent_id` is the registration id (`runtime_id` today); the role name stays what it is, the agent's *type*. Proposed:

- **Audit events:** `agent_id`, `agent_version` and `agent_fingerprint` become fields of `_AuditEventBase`, filled where `role` and `deployment` already are. They are additive and nullable, so old rows and old sinks are unaffected; whether they become columns or live in `payload` is an implementation choice (columns can be queried; `payload` needs no migration).
- **Logs:** the `runtime.turn_usage` line and the other `runtime.*` events carry the same three values.
- **Metrics:** `agent_version` becomes a label on the four metrics that already carry `runtime_id` (turns, duration, tokens, cost), because those are the ones that compare two versions. The fingerprint is *not* a label: it changes on every edit and the metric labels are reviewed to stay bounded. It goes on a single `agent_build_info{runtime_id, agent_version, fingerprint} 1` gauge, the usual way to attach build metadata without multiplying series. Tool-level metrics keep their `tool` label; adding a version to them would multiply series for a comparison nobody needs.
- **Checkpoints:** the version that wrote a checkpoint is recorded in its metadata (this is D4's input).

The metrics part is the label contract #173 (per-agent metrics) should adopt, so that work is not done twice. Cardinality stays bounded because a deployment runs a handful of versions at once.

### D4 — Deploy and rollback

**Pinning.** A registration in a served deployment must state the version it expects **and** the fingerprint. At boot the lifespan resolves each agent (loading its skills, as in D1), compares both, and refuses to start on either mismatch, naming the id, the pinned and the resolved values. A version-only pin is not enough: it accepts a changed definition that kept its version, which is the failure this ADR exists to close. This is the same stance as an unregistered id or a missing `DEPLOY_GRANTS` entry: a deployment does not silently run something other than what its operator wrote down. The one exception is an explicit development opt-out (a registration marked unpinned on purpose, refused when the deployment declares itself production, and logged at boot), so in-process use, notebooks and tests are not forced to keep pins. Mandatory pins change today's behavior, where registrations carry none, so they ship with a release note and one release of warning before boot fails. The exact syntax for `AGENT_REGISTRATIONS` (its values are strings today), how a deployment declares its mode, and the warning period are left to the implementation issue. The CLI's `release` output prints the pin to paste.

**Rollback.** Rollback is a redeploy of the previous revision *and* restoring both previous pins, version and fingerprint. The boot check refuses a revision whose fingerprint does not match the pins, so a partial rollback fails loudly instead of running something in between. A definition is files in git, so an older version is recoverable with no copy stored anywhere else, and the pinned fingerprint proves the redeploy reproduced the released definition exactly, not an approximation of it. The alternative, a frozen resolved snapshot per release that could be loaded without the old code, was considered and deferred: it needs a serializer and a loader for `AgentDefinition` (skill contents, command tools, model) that must re-run every load-time validation, a second path into the same invariants for a benefit that a git revision already gives.

**Threads across a version change.** Today a checkpointed thread holds messages only and the system prompt is applied at call time, so a resumed conversation runs under the new definition immediately. What the version change means for it:

- **MINOR:** threads continue. The same customer keeps the same conversation.
- **MAJOR:** the recommendation is to continue as well, and to record the transition. Authority is not stored in a thread: Layer 2 revalidates every sensitive call against the *current* `deploy_grant_ceiling`, so an old thread cannot carry a removed permission forward. What can go wrong is coherence: history that shows tool calls to a tool the new version no longer has. That risk is real, provider-dependent, and untested here; it must be exercised by an implementation test before it is called safe.
- **The alternative** (a fresh thread namespace per MAJOR) is cleaner for the model and worse for the customer: every MAJOR would reset every open conversation. If the owner prefers it, it is a per-agent choice (`continue` or `new_thread` on MAJOR), not a platform default.
- **Long-term memory** (ADR-002 A.3, G.25) does not exist yet. When it does it is keyed by `agent_id`, not by version, and a change in what a memory *means* is that feature's problem. #24 is the point to add the version to checkpoint metadata, so the data to decide is already there.

### D5 — Lifecycle states and tooling

```
draft ──eval passes for this fingerprint──▶ evaluated ──release──▶ released ──deprecate──▶ deprecated
  ▲                                              │                     │
  └───── any edit to the agent or its scenarios ─┴─────────────────────┘ (the edited definition is a new draft)
```

- **draft:** the definition differs from the lock, or has no passing evaluation for its fingerprint and current scenario digest.
- **evaluated:** the scenarios passed their gates for the current fingerprint.
- **released:** version bumped as D1 requires, evaluated, locked. This is the only state a production pin should name.
- **deprecated:** still resolvable, so a rollback target does not vanish, but a pin to it warns at boot and a new deployment is discouraged. Removing a version is git's job.

The state lives in the lock file, not in a database, so it travels with the code it describes.

| Command | Does |
|---|---|
| `agents-system agent check` | Offline D1 check. What CI runs on every PR. |
| `agents-system agent eval` | Runs the agent's scenarios, records the result against the fingerprint. |
| `agents-system agent release` | Requires a passing evaluation for the current fingerprint and the bump D1 demands; writes the lock. |
| `agents-system agent diff [A] [B]` | Section-by-section difference between the lock and the working definition, or between two locks, with the bump each difference requires. |
| `agents-system agent deprecate` | Marks a released version deprecated. |

`check` and `diff` are pure and offline, so they must work from the light install (#188) without the live-eval or server extras. This also introduces the first console script in the package.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Version only, enforced by review** (today) | The reason for this ADR: nothing failed when the version stayed put. |
| **A git SHA as the version** | Excellent for traceability, silent on compatibility: it cannot say that a change widened authority, and it changes on unrelated commits. It is useful as *extra* metadata, not as the version. |
| **Fingerprint only, no human version** | A hash cannot express MAJOR versus MINOR, and nobody can say "roll back to 2.1" in an incident. |
| **Hash the files, not the resolved definition** | Misses a change inherited through `extends:` or from the base contract, and reports whitespace as a change. |
| **A database-backed registry with versions stored and switched at runtime** | More flexible and a different product: the definition stops being reviewed files, and it fights the explicit-grant model. Deferred until there is a demand for hot switching. |
| **A version label on every metric, fingerprint included** | Multiplies series, and the fingerprint changes on every edit. The info-gauge pattern carries the same information at fixed cost. |

## Consequences

- If accepted, ADR-004 D8's sentence excluding importer agents from Guard 1 is amended: they become governed by the lock mechanism instead. The original text stays as history, with a pointer, as ADR-004 did for ADR-002.
- Every agent gains a lock file and a version discipline. The cost is a bump on each change, which is the point; the check names what moved so the bump is not guesswork.
- A platform base-contract edit forces a MINOR bump in every agent that has a lock. Batched, that is one mechanical PR per consumer; it is also exactly the moment a consumer should re-run its evals.
- Pins become mandatory for served deployments: existing registrations need a version and a fingerprint when this ships, after a warning release.
- Audit rows and metric series grow by the new fields and the version label. Additive; no existing query breaks.
- The eval runner gains a new kind of target (an agent folder), which is shared work with any other feature that wants to evaluate importer agents.

## Open questions for the owner

1. **Version shape.** Enforce `MAJOR.MINOR` digits? Existing custom agents with a free-form `version:` would fail `check` until they adopt it.
2. **Which model gates a release** when the agent declares its own `model`: the declared one (most faithful), or the reference model (comparable across agents)?
3. **The scenario floor.** Is "at least one guardrail scenario for any agent with a T2/T3 tool" the right minimum, or too strict for a first release?
4. **MAJOR across live threads:** continue (recommended) or a fresh namespace, and is it a platform default or a per-agent setting?
5. **Pins:** this ADR proposes that served registrations must pin version and fingerprint, with an explicit development opt-out. Confirm, and how long a warning period before boot fails?
6. **Predefined roles:** retire `EXPECTED_ROLE_SURFACE` in favor of locks, or keep both?

## Follow-ups (only after acceptance)

- The fingerprint and lock module, with `agent check`; the CI job, comparing against the base branch's lock, replacing Guard 1's importer exclusion.
- Eval runner support for an agent folder; `agent eval` and `agent release`.
- Version and fingerprint on audit events, logs and metrics, aligned with #173.
- Pin syntax (version and fingerprint, development opt-out) and boot check.
- Version in checkpoint metadata, with #24.
- `agent diff` and `agent deprecate`; the retirement of the hand-kept role snapshot.
