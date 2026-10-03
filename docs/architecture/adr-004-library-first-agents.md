# ADR-004 — Library-first custom agents

**Status:** Accepted · **Date:** 2026-09-26 · **Amends:** ADR-002 A.1, D, C.15, E.18, F

## Summary

`agents_system` no longer only serves its 8 packaged predefined roles. An
importer builds a custom `Agent` — from its own folder, from Python
parameters, or both — that extends the generic agent or any predefined role,
resolves through the exact same `AgentDefinition`/`AgentRuntime` pipeline,
and inherits every library invariant (the base contract, capability tiers,
the `untrusted_input`⊥T3 barrier, the deploy-time grant ceiling) for free.
`create_app` registers agents under deployer-chosen ids instead of parsing
them out of a `{deployment}__{role}` string. This shipped across ten PRs
(#68, #70, #85, #91, #92, #95, #98, #101, #103, plus this one), two of them
(#94 for issue #93, #97 for issue #75) hardening the design below in
response to security review rather than following it as originally written.

## Context

Before this change, every resolution path in `harness/loader.py` and the
runtime-id scheme in `main.py`/`integration/openai_adapter.py` hard-coded
"a role is a name under `platform_root/roles`." ADR-003 (permission-model,
released as v0.2.0) had just landed the safety layer — the `Permission`/
`Tier` hierarchy and the explicit `DEPLOY_GRANTS` deploy-time ceiling — an
importer-defined agent needed to inherit without any agent-specific code.
This ADR records what actually shipped to make that possible, correcting
`design.md`/`proposal.md` where the merged implementation diverged from the
original plan.

## Decision

### D1 — `RoleLocator`: platform role name | importer folder | inline definition

`harness/loader.py` gains `RoleLocator = str | FolderLocator | InlineLocator`.
A bare `str` is unchanged — every existing platform-role call site needed
zero edits. `FolderLocator(path, root, overrides)` reads an importer
folder's `role.md`/`manifest.md`/`policy.md` the same way a platform role's
is read; `InlineLocator(raw, parent)` wraps an already-built `RawDefinition`
and touches no disk. `_load_role_files`, `_resolve_role_chain`,
`load_generic` and `resolve()` dispatch on the union; `merge()`, the
injector and `_append_base_contract` are untouched, which is why every
library invariant applies identically regardless of agent shape (#68).

### D2 — `extends:` fails loud instead of silently collapsing

`_extends_target` no longer silently strips an unresolvable value down to a
same-named platform role. A value is placed in exactly one of two spaces —
the platform role tree, or the current agent's own importer root — or it
raises `DefinitionError`; an absolute path, or one whose resolved target
lands outside that root, is rejected the same way, checked after resolution
rather than by rejecting a `..` segment on sight (a `../base-support`
sibling reference inside the same importer root is legitimate). One
deviation from `design.md`'s own code block: a relative `extends:` value
resolves against the declaring agent's own folder, not against the whole
importer root `resolve()` was given — the design's worked example already
assumed this; the code block it also shipped did not, and was never
corrected (#70).

### D3 — `Agent`: a frozen, deep-immutable Python builder

> **Amended by ADR-010:** `extends` by a concrete predefined-role name is
> deprecated: it emits a `PredefinedRoleDeprecationWarning` naming the
> reference agent in `examples/agents/` to copy, and the next breaking
> release removes it. `extends="agent"` (the default), `extends="base"` and
> another `Agent` are unchanged. See [ADR-010 — Capability-based evals, with predefined roles as reference agents](adr-010-capability-evals-and-reference-agents.md).

`agent/spec.py`'s `Agent` (exported as `agents_system.Agent`) builds an
agent from Python parameters, from `Agent.from_folder(path, **overrides)`,
or both — `overrides` replaces, never merges, the folder's own field.
`extends` accepts a predefined-role name (resolved lazily, D2's algorithm)
or another `Agent` (resolved eagerly — cycle-free by construction, since
Python cannot build an object before its own dependencies exist). It ships
genuinely frozen: every container field is deep-copied into an immutable
shape at construction, not only declared `frozen=True` at the top level
(#85).

An importer-authored definition anywhere in an `extends:` chain — not only
the leaf — is held to its parent's effective `autonomy`, `execution_limits`,
audit redaction and escalation conditions before it is folded in
(`_apply_importer_ceiling`); a parentless importer root is held to the
platform defaults. This closed a HIGH-severity finding on #85's review that
a custom agent could otherwise widen scope through `extends` in a way a
predefined-role child never could — platform-to-platform folds stay
additive-only, unaffected. Issue #93 then found the ceiling itself covered
only `autonomy`/`execution_limits`, and that `Agent(...)`'s own parameters
skipped the YAML path's type checks entirely — a bare string for `tools`
would iterate into its characters, and a `permissions` removal directive
without `inherit: true` silently resolved to the parent's full set. Neither
gap is described in `design.md`'s D3, which predates both: `_checked` now
runs every `Agent`/`from_folder`-override field through the same parsers
`policy.md`/`manifest.md` use, and the ceiling also covers audit redaction
and unions escalation conditions rather than replacing them (#94).

### D4 — Skills: inline text, then the agent's own folder, then deployment

`_load_skills` resolves each declared skill name against, in order:
`Agent(skill_contents={...})`'s inline text, `Agent.from_folder(path)`'s own
`path/skills/`, then — unchanged — a client deployment's `skills/` folder
for a predefined role. `docs/platform/deployment.md`'s "there are no generic
skills" and `docs/platform/role.md`'s aspirational `skills/` mention are
corrected to state this (#91).

### D5 — Registration: deployer-chosen ids replace `{deployment}__{role}`

`create_app` gains `agents: Mapping[str, Agent | str] | None`,
`grants: Mapping[str, Sequence[str]] | None` and
`clients: Mapping[str, str] | None`. Every id is opaque — at most 64
`_SAFE_SEGMENT` characters, never parsed — and every id `WHATSAPP_RUNTIME_ID`
or `ADAPTER_RUNTIMES` names must be registered, or boot fails naming it.
`clients` is rejected against an `Agent`-valued id: deployment overrides
stay predefined-role-only. When `agents` is omitted, `lifespan()` falls back
to the new `Settings.agent_registrations` field (`AGENT_REGISTRATIONS`,
`{id: "role"}` or `{id: "role@client"}`), validated through the identical
path (#92, #95, #98). `to_model_id`/`parse_model_id` and the inline
`model_id.split("__", 1)` parser are deleted, not consolidated;
`tests/test_no_duplicated_runtime_id_parsing.py` is an AST scan pinning that
no third copy of that parsing reappears.

### D6 — Runtime id validation, and where the new code lives

`_validate_runtime_id` (`main.py`) reuses `loader._SAFE_SEGMENT`'s character
class for every id, whichever of `agents`/`grants`/`clients`/
`agent_registrations` it came from. `Agent` lives at
`src/agents_system/agent/spec.py`, alongside `agent/graph.py` — not a
sibling `agent.py` file, which cannot coexist with the already-existing
`agent/` package.

### D7 — `examples/demo/app.py`, not a packaged entry point

> **Amended by ADR-010:** the demo moves to registering reference agents from
> `examples/agents/` with `create_app(agents=...)`, not a predefined-role
> name. See [ADR-010 — Capability-based evals, with predefined roles as reference agents](adr-010-capability-evals-and-reference-agents.md).

`src/agents_system/demo.py` moved to `examples/demo/app.py`, byte-identical
apart from its module docstring. `examples/` ships no `__init__.py` and is
outside the installed package, so the documented invocation is a plain
`python examples/demo/app.py`, not a `python -m` entry (#101). `demo.py`'s
own `build_app` call needed zero changes — it already omits `agents`, so it
keeps using the `AGENT_REGISTRATIONS` fallback path.

### D8 — Guard 1: a reviewed snapshot, plus a required CI check

> **Amended by ADR-010:** once the concrete roles leave `platform/roles/`,
> Guard 1 narrows to the generic parents: `agent` is the only baseline other
> agents build on, and the reference agents carry no compatibility promise.
> See [ADR-010 — Capability-based evals, with predefined roles as reference agents](adr-010-capability-evals-and-reference-agents.md).

A predefined role's `tools`/`permissions` surface is a shared safety
baseline other agents' `extends:` chains build on, so a change to it is
enforced as a MAJOR (breaking) change through two independent halves,
because the two checks run at different times in this repo's actual
pipeline. `RoleGovernanceSnapshot`/`EXPECTED_ROLE_SURFACE`
(`tests/platform_role_contract.py`) is a checked-in, reviewed literal per
predefined role; the contract test in `tests/test_role_governance_contract.py`
fails when a role's resolved `(tools, permissions)` diverges from its
snapshot unless the role's own `version:` frontmatter took a MAJOR bump.
Separately, the `predefined-role-governance-marker` job
(`.github/workflows/ci.yml`, `scripts/check_role_governance_marker.py`)
requires a conventional-commit `BREAKING CHANGE:`/`!` marker on any PR
touching `platform/roles/**/manifest.md` — a `CHANGELOG.md` entry cannot
exist yet at PR time, since release-please only writes one in its own later
release PR, so the marker is the enforceable, correctly-timed equivalent.
#103 shipped this job running, but not required: `main`'s branch protection
still listed only `ci`/`secret-scan`/`dependency-audit`, so a manifest
change without the marker could still merge — the pytest half alone
blocked it. Issue #104 closed this gap as a repository-settings-only
change, with no PR (`required_status_checks.contexts` now also lists
`predefined-role-governance-marker`, confirmed by reading branch protection
back). **Both `ci` and `predefined-role-governance-marker` are required
GitHub status checks on `main` today.** An importer-defined `Agent`
extending a predefined role is explicitly out of Guard 1's scope: it has no
name in `PINNED_ROLES`, so it is not, and can never become, a governance
subject through this mechanism.

## ADR-002 amendments

This ADR adds a short pointer at each of the following ADR-002 sections,
without deleting or rewriting their original text — that text remains the
historical record of the eight-predefined-role-only world; this ADR is what
actually ships on top of it now:

- **A.1** — role = class now includes an importer-authored `Agent`, not only
  a predefined role.
- **D** — the inheritance tree an `extends:` chain can reach now includes an
  importer folder or an inline definition, not only the 8 predefined roles.
- **C.15** — the platform-generic reference backends are agent-shape-
  agnostic, available to a custom `Agent` exactly as to a predefined role.
- **E.18** — the live evaluation pipeline runs against a resolved
  `AgentDefinition`, not a role name, so it applies to a custom agent
  unchanged.
- **F** — the skills-location fix (D4) and the predefined-role terminology
  rename (#101) touch the same files this section's doc-hygiene work does.

## Open follow-ups

- `Agent` does not expose declarative `command_tools` (ADR-002 C.12) — an
  importer agent cannot declare one. Left for a follow-up change
  (`design.md`'s own Open Questions).
- Per-principal identity (issue #15, ADR-002 A.2) stays role/deploy-scoped;
  untouched by this change.
- `permission-hierarchy`'s capability spec has not been promoted from its
  archived change folder to `openspec/specs/` — a process-only gap, not
  fixed here.

## Corrections

- `design.md` D2's own code block resolves a relative `extends:` value
  against the whole importer root; the shipped rule resolves it against the
  declaring agent's own folder first (#70) — see D2 above. The code block
  was never updated to match.
- `design.md`'s D3 predates the importer safety ceiling's full scope and the
  `Agent` parameter type-check hardening entirely — both were added by
  security review (#93), not planned, and are documented here instead.
- `design.md`'s own Threat Matrix already listed the importer-folder
  containment gaps (symlink escape, TOCTOU, command-tool revalidation) as
  applicable risks with planned tests; #97 is the PR that actually closed
  them, in this same chain, not a deferred follow-up.
- `design.md`'s D8 describes Guard 1's marker check as enforced once #103
  merged; #103's own PR body already flagged that it was not yet a required
  status check, and #104 — a repository-settings-only change, not a PR —
  is what actually made it one. D8 above documents both steps.
- `proposal.md`'s Risks table flagged `exploration.md`'s description of a
  stale pre-ADR-003 auto-grant in `main.py`; the shipped code already used
  the current `DEPLOY_GRANTS`-keyed model throughout this change, confirmed
  by direct read at each PR rather than assumed from that stale note.
- Every PR in this chain exceeded its `design.md` Testing Strategy estimate
  and the ~400-line default budget (PR2: 1830 lines; PR4a-ii: 1090; PR4b:
  1336) — overwhelmingly tests-first coverage per named scenario. Each
  carries its own `size:exception` recommendation in `state.yaml` rather
  than a trimmed test suite.
