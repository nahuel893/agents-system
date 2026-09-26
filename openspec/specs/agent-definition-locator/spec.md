# Agent Definition Locator Specification

## Purpose

Today an agent definition can only be reached one way: a name looked up
under `platform_root/roles`. This capability widens that single path into a
locator with three source kinds — a predefined-role name (today's behavior,
unchanged), an importer-supplied folder, or an inline Python-built
definition — so an importer can define and resolve a custom `Agent` through
the exact same resolution/merge/injection pipeline that already proves every
library invariant for the eight packaged roles.

Terminology used throughout this spec: **predefined role** refers to one of
the eight packaged roles shipped under `platform_root/roles` (previously
called "generic roles" in some docs/comments). **Generic agent** refers
specifically to the abstract `base -> agent` tree every predefined role
extends — that tree keeps the word "generic"; only the distinguishing usage
for the eight packaged roles is renamed.

This spec does not restate the `Permission`/`Tier` model, capability tiers,
or the ADR-003 grant rules (R1-R4). Every requirement below that touches
permissions defers to `openspec/specs/permission-hierarchy/spec.md`, which
is the source of truth for that model.

## Requirements

### Requirement: Agent locator discriminates exactly three source kinds

The system MUST resolve an agent definition through exactly one of three
locator kinds: a **predefined-role locator** (a name resolved under the
platform's own role tree), an **importer-folder locator** (a caller-supplied
directory path), or an **inline locator** (an already-built definition
supplied directly from Python, with no disk read). Every locator kind MUST
flow through the same resolution, merge, and injection code path.

#### Scenario: A predefined-role locator is used

- GIVEN a locator naming `"sales-agent"` with no folder path and no inline definition
- WHEN it is resolved
- THEN the system reads `role.md`/`manifest.md`/`policy.md` from under `platform_root/roles/sales-agent`, exactly as before this change

#### Scenario: An importer-folder locator is used

- GIVEN a locator carrying a caller-supplied directory path outside `platform_root`
- WHEN it is resolved
- THEN the system reads `role.md`/`manifest.md`/`policy.md` from that directory instead of `platform_root/roles`

#### Scenario: An inline locator is used

- GIVEN a locator carrying an already-built definition object with no folder path
- WHEN it is resolved
- THEN the system skips every disk read and proceeds directly to merge/injection using the supplied definition

### Requirement: Predefined-role resolution is behavior-unchanged

For the predefined-role locator branch, the system MUST produce byte-for-byte
the same `AgentDefinition` it produced before this change for every existing
predefined role and every existing deployment override, given the same
inputs.

#### Scenario: All eight predefined roles resolve unchanged

- GIVEN the existing predefined-role/deployment-override regression suite
- WHEN it is run after the locator generalization ships
- THEN every test passes with no change to expected `AgentDefinition` output

#### Scenario: A deployment override still narrows only

- GIVEN a deployment override manifest for a predefined role
- WHEN it is merged through the predefined-role locator branch
- THEN the subtractive-only tools invariant (an override's tools MUST be a subset of the generic role's tools) is enforced exactly as before

### Requirement: Importer-folder locator reads the same three-file contract

An importer-folder locator MUST read `role.md` (identity/prose), `manifest.md`
(capabilities), and `policy.md` (behavior) from the supplied directory, using
the identical parsing rules the predefined-role branch already applies
(frontmatter parsing, design-notes stripping, base-contract handling).

#### Scenario: A well-formed importer folder resolves

- GIVEN an importer folder containing valid `role.md`/`manifest.md`/`policy.md`
- WHEN `Agent.from_folder(path)` resolves it
- THEN the resulting `AgentDefinition` has the same shape (all fields populated) as one produced from a predefined-role folder

#### Scenario: A malformed importer folder fails the same way a malformed platform folder would

- GIVEN an importer folder missing `policy.md`
- WHEN it is resolved
- THEN resolution MUST raise the same class of error (`DefinitionError` or its documented file-parsing subclass) that a predefined role with a missing file would raise, naming the missing file and the folder path

### Requirement: `extends:` resolves a generic-or-predefined parent unchanged

An `extends:` value that names the generic agent (`platform/roles/agent`) or
any predefined role — in either the bare-name form (`extends: agent`) or the
platform-rooted path form (`extends: platform/roles/agent`) — MUST resolve
through the predefined-role locator branch exactly as it does today, for
both predefined-role manifests and importer-defined agents.

#### Scenario: An importer agent extends the generic agent by bare name

- GIVEN an importer folder's `manifest.md` declares `extends: agent`
- WHEN the chain is resolved
- THEN the parent resolves to `platform/roles/agent`, identically to a predefined role declaring the same value

#### Scenario: An importer agent extends a predefined role by path form

- GIVEN an importer folder's `manifest.md` declares `extends: platform/roles/sales-agent`
- WHEN the chain is resolved
- THEN the parent resolves to the predefined `sales-agent` role, and the importer agent's manifest folds on top of it using the same additive-inheritance rules a predefined-role child uses today

### Requirement: `extends:` resolves an importer-relative parent

An `extends:` value MAY reference another importer-defined agent by a path
relative to the importer's own locator root. When it does, the system MUST
resolve that parent from the importer's own root, not from `platform_root`.

#### Scenario: An importer agent extends a sibling importer agent

- GIVEN two importer folders under the importer's own root, `agents/base-support/` and `agents/vip-support/`, where `vip-support/manifest.md` declares `extends: ../base-support`
- WHEN `vip-support` is resolved
- THEN the parent resolves to `agents/base-support` under the importer's own root, and the child's manifest folds on top of it

#### Scenario: An inline definition extends an importer-folder agent

- GIVEN an inline Python-built definition declares `extends` pointing at an importer folder path
- WHEN it is resolved
- THEN the parent resolves from that importer folder, not from `platform_root`

### Requirement: `extends:` fails loudly when unplaceable in either locator space

An `extends:` value that cannot be placed in one of the two allowed locator
spaces — the platform-role tree, or the importer's own root — MUST raise
`DefinitionError` naming the unresolvable `extends:` value and the two
allowed spaces it was checked against. The system MUST NOT silently
re-resolve such a value by keeping only its last path segment and searching
`platform_root/roles` for a same-named role (the previous behavior of
`_extends_target`, `harness/loader.py:869-876`, which returned
`str(raw).strip().rstrip("/").rsplit("/", 1)[-1]` unconditionally).

#### Scenario: A path that matches no importer file and no platform role fails loudly

- GIVEN an importer manifest declares `extends: some/nonexistent/path/custom-agent`
- WHEN the chain is resolved
- THEN resolution MUST raise `DefinitionError` naming `"some/nonexistent/path/custom-agent"`; it MUST NOT silently collapse to `custom-agent` and search `platform_root/roles` for it

#### Scenario: A last-segment collision with a real predefined role no longer silently extends the wrong parent

- GIVEN an importer manifest declares `extends: some/importer/path/agent`, where `agent` happens to also be the name of the generic agent under `platform_root/roles`, and `some/importer/path/agent` does not exist under the importer's own root
- WHEN the chain is resolved
- THEN resolution MUST raise `DefinitionError` naming the full unresolved value; it MUST NOT silently extend `platform/roles/agent` instead — this is the regression this requirement exists to close

#### Scenario: A value that resolves unambiguously in exactly one space still succeeds

- GIVEN an `extends:` value that resolves to a real folder under the importer's own root and does not collide with any platform-role name
- WHEN the chain is resolved
- THEN resolution succeeds using that importer-root match

### Requirement: `Agent.from_folder` produces the loader's `AgentDefinition` shape

`Agent.from_folder(path, ...)` MUST read the three-file contract from `path`
and produce the identical `AgentDefinition` shape (the same fields, the same
types) that the predefined-role locator branch produces from a
`platform_root/roles/<name>` folder, including the fully-resolved
`extends:` chain, the composed `system_prompt`, and every manifest/policy
field.

#### Scenario: from_folder output shape matches a predefined role's

- GIVEN a structurally equivalent importer folder and a predefined-role folder (same fields, no `extends:` chain)
- WHEN both are resolved
- THEN the two resulting `AgentDefinition` objects have identical field sets and types, differing only in role-specific content (name, tools, etc.)

### Requirement: `Agent(...)` Python-parameter construction resolves without disk

`Agent(name=..., extends=..., tools=..., permissions=..., skills=..., ...)`
MUST accept the same set of fields the three-file contract can express,
supplied directly as Python parameters, and MUST produce an
`AgentDefinition` through the same merge/injection pipeline without reading
any file for the agent's own content. `extends:` supplied this way follows
the same two-allowed-locator-space resolution and fail-loud rule as the
folder form.

#### Scenario: A pure-Python agent resolves with no folder

- GIVEN `Agent(name="triage-bot", extends="agent", tools=["catalog_search"], permissions=["read:catalog"])` with no folder path supplied
- WHEN it is resolved
- THEN the system reads zero files for the agent's own content, folds the declared fields onto the resolved `extends:` parent, and produces a complete `AgentDefinition`

#### Scenario: A pure-Python agent still receives every library invariant

- GIVEN the pure-Python agent from the scenario above
- WHEN it is resolved
- THEN it receives the base prompt contract, tier/T3 enforcement, and `untrusted_input` handling identically to a folder-defined agent (see the invariant requirements below)

### Requirement: Folder content and Python parameters compose, with parameters as the override layer

`Agent.from_folder(path, **params)` and any Python parameters passed
alongside a folder locator MUST compose: content parsed from the folder
populates the definition first, and then, field by field, an explicitly
supplied Python parameter replaces the corresponding folder-derived value. A
field never explicitly supplied as a Python parameter keeps its
folder-derived value. This precedence is a spec-level assumption made
explicit for testability — the proposal names this combination as required
("both AND together... folder + params overriding/extending folder
content") without fully specifying per-field precedence; sdd-design MAY
refine or confirm this rule before implementation.

#### Scenario: An explicit parameter overrides the same folder-declared field

- GIVEN an importer folder whose `manifest.md` declares `tools: [catalog_search]`, and a call `Agent.from_folder(path, tools=["catalog_search", "order_writer"])`
- WHEN the definition is resolved
- THEN the effective `tools` field is `["catalog_search", "order_writer"]` (the explicitly passed value), not the folder-only value

#### Scenario: A field not passed as a parameter keeps the folder's value

- GIVEN the same folder and a call `Agent.from_folder(path, tools=["catalog_search", "order_writer"])` that does not pass `permissions`
- WHEN the definition is resolved
- THEN the effective `permissions` field is exactly what the folder's `manifest.md` declared

### Requirement: Additive inheritance from the generic agent or any predefined role (Option B)

An importer-defined agent (folder, inline, or a composition of both) MUST be
able to declare `extends:` targeting either the generic agent
(`platform/roles/agent`) or any predefined role, and the fold direction MUST
be additive: the importer agent's own declared fields layer on top of the
resolved parent using the same fold rules a predefined-role child already
uses (e.g. `tools`/`permissions` extend the parent's set unless the child
explicitly narrows them). Multiple inheritance MUST NOT be supported — a
child locator declares at most one `extends:` value.

#### Scenario: An importer agent extending sales-agent inherits its tool surface

- GIVEN an importer folder declares `extends: platform/roles/sales-agent` and adds one new tool
- WHEN it is resolved
- THEN the resulting agent's tool surface is `sales-agent`'s tools plus the one new tool

#### Scenario: An importer agent extending the generic agent starts from the minimal floor

- GIVEN an importer folder declares `extends: agent` (the generic agent) and no predefined-role ancestor
- WHEN it is resolved
- THEN the resulting agent inherits only the generic agent's floor (`session_state`/`escalation_notifier` tools, `read:session`/`send:escalation` permissions, `supervised` autonomy floor) before its own declared fields fold on top

### Requirement: The base prompt contract is appended last regardless of locator kind

Every resolved `AgentDefinition`, regardless of which locator kind produced
it, MUST end its `system_prompt` with the ADR-002 B.8/B.9 six-clause base
contract as the final block, applied by the same mechanism the
predefined-role path already uses on every `resolve()` exit path.

#### Scenario: An importer-folder agent's prompt ends with the base contract

- GIVEN an importer-folder agent with its own role prose and one skill
- WHEN it is resolved and its runtime prompt is composed
- THEN the base contract block is the last block in the composed prompt, after the role prose and skill content

#### Scenario: An inline agent's prompt ends with the base contract

- GIVEN an inline Python-built agent with no folder-sourced prose beyond what Python parameters supplied
- WHEN it is resolved
- THEN its `system_prompt` still ends with the base contract block

### Requirement: untrusted_input ⊥ T3-tier permission exclusion applies regardless of locator kind

Every resolved `AgentDefinition`, regardless of locator kind, MUST be
subject to ADR-002 C.11 / ADR-003 R4 exactly as specified in
`openspec/specs/permission-hierarchy/spec.md`'s "untrusted_input roles hold
no T3 permission (R4)" requirement: an agent whose resolved
`untrusted_input` is `true` MUST NOT resolve to an effective permission set
containing any T3-tier permission, regardless of which locator kind
produced the definition.

#### Scenario: An importer-folder agent declaring untrusted_input and a T3 permission fails to load

- GIVEN an importer folder's `policy.md` declares `untrusted_input: true` and its `manifest.md` declares a permission resolving to a T3 class (e.g. `exec:command`)
- WHEN the definition is loaded
- THEN load MUST raise `UntrustedInputGrantError`, identically to a predefined role in the same situation

#### Scenario: An inline agent honors the same exclusion

- GIVEN an inline Python-built agent with `untrusted_input=True` and a T3-tier permission in its `permissions` parameter
- WHEN it is resolved
- THEN resolution MUST raise `UntrustedInputGrantError`, per the same rule

### Requirement: Capability tiers and the T3 sandbox gate apply regardless of locator kind

Every tool an importer-defined agent declares MUST be subject to the same
`ToolSpec` tier ceiling/floor rules (R2a/R2b) and the same injector-level T3
sandbox barrier defined in `openspec/specs/permission-hierarchy/spec.md`,
with no locator-kind-specific exemption.

#### Scenario: An importer agent cannot equip a tool its declared permissions don't cover

- GIVEN an importer-folder agent declares a tool requiring `exec:command` but does not declare `exec:command` among its permissions
- WHEN its runtime is built
- THEN the tool is denied by the injector, exactly as it would be for a predefined role in the same situation

#### Scenario: The T3 sandbox barrier is not bypassable by choosing the inline locator

- GIVEN an inline Python-built agent requests a T3 tool without the matching T3-tier deploy grant
- WHEN a turn attempts to call that tool
- THEN Layer-2 revalidation denies it, identically to any other locator kind

### Requirement: Skills load from an importer agent's own folder

An importer-folder locator's own `skills/` subdirectory MUST be a valid
source for the skill content its `manifest.md`/Python parameters declare,
independent of any deployment. This is new, additive behavior — it does not
replace or alter the existing deployment-only skills mechanism for
predefined roles (see the following requirement).

#### Scenario: An importer agent's own skills/ resolves with no deployment

- GIVEN an importer folder declares `skills: [pricing-nuance]` and contains `skills/pricing-nuance.md`
- WHEN its runtime is built with no `client`/deployment supplied
- THEN the skill content loads from the importer folder's own `skills/pricing-nuance.md`, and the deployment-only `FactoryError` ("declares skills but no client deployment") is NOT raised for this agent

#### Scenario: A missing importer-folder skill file fails the same way a missing deployment skill file does

- GIVEN an importer folder declares `skills: [missing-skill]` but has no `skills/missing-skill.md`
- WHEN its runtime is built
- THEN it MUST raise `FactoryError` naming the missing skill and its expected path, in the same message shape the deployment-skills path already uses

### Requirement: Skills load from inline Python-supplied content

Skill content MAY be supplied directly as Python parameters (not read from
any file) when an agent is constructed via `Agent(...)` or via
`Agent.from_folder(path, **skill_overrides)`. Inline-supplied skill content
composes with folder-sourced skill content using the same override
precedence as other fields (see "Folder content and Python parameters
compose").

#### Scenario: Inline skill content is used verbatim

- GIVEN `Agent(name="triage-bot", extends="agent", skills={"tone": "Always answer in a formal register."})`
- WHEN its runtime is built
- THEN the `tone` skill's content is exactly the inline-supplied string, with no file read

#### Scenario: Inline skill content overrides a same-named folder skill

- GIVEN an importer folder's `skills/tone.md` exists, and `Agent.from_folder(path, skills={"tone": "override text"})` is called
- WHEN its runtime is built
- THEN the `tone` skill's effective content is `"override text"`, not the folder file's content

### Requirement: Predefined-role deployment-only skills remain unchanged

For predefined roles, the existing deployment-only skills mechanism
(`deployments/{client}/{role}/skills/`, raising `FactoryError` when
`client is None` and skills are declared) MUST continue to behave exactly as
before this change. Predefined roles do not gain a "resolve skills from
their own platform-role folder" capability — that folder-sourcing path is
reserved for importer-defined agents.

#### Scenario: A predefined role with no deployment and declared skills still fails the same way

- GIVEN a predefined role declares skills and no `client`/deployment is supplied
- WHEN its runtime is built
- THEN it raises `FactoryError` exactly as it did before this change

#### Scenario: A predefined role's own platform-role folder is never treated as a skills source

- GIVEN a predefined role's own `platform_root/roles/<name>/` folder happens to contain a `skills/` subdirectory
- WHEN its runtime is built for a given deployment
- THEN skills load from `deployments/{client}/{role}/skills/`, never from `platform_root/roles/<name>/skills/`

### Requirement: Deployment-override subtractive semantics are unaffected by the locator generalization

`load_override`'s enforcement that a deployment override's `extends:` (when
declared) MUST equal its own role type, and that an override's `tools` MUST
be a subset of the generic role's tools, MUST continue to apply, unchanged,
to every predefined-role deployment override after the locator
generalization ships.

#### Scenario: A deployment override still cannot elevate tools

- GIVEN a deployment override manifest declares a tool the generic role does not grant
- WHEN it is merged
- THEN merge MUST reject it exactly as before this change

#### Scenario: A deployment override still cannot redirect its own extends target

- GIVEN a deployment override's manifest declares `extends:` pointing at a role type other than its own
- WHEN it is loaded
- THEN load MUST reject it exactly as before this change

### Requirement: Predefined-role discovery stays scoped to the platform-role tree

Any discovery mechanism that walks the set of predefined roles (used by boot
guards and by the governance contract check in the
`predefined-agent-governance` capability) MUST remain scoped strictly to
`platform_root/roles`. The locator generalization MUST NOT cause
importer-defined agents (folder or inline) to appear in that discovery set,
whether or not the importer's folder happens to sit near `platform_root` on
disk.

#### Scenario: An importer folder placed adjacent to platform_root is not discovered as a predefined role

- GIVEN an importer folder is placed as a sibling directory to `platform_root/roles`, not inside it
- WHEN predefined-role discovery runs
- THEN that importer folder does not appear in the discovered set

#### Scenario: Discovery result is unchanged in size after this change ships

- GIVEN the eight predefined roles under `platform_root/roles` before this change
- WHEN discovery runs after the locator generalization ships, with no importer agents defined anywhere
- THEN discovery still returns exactly the same eight roles

### Requirement: Error and boot-failure messages name predefined roles by their new term

Any error or boot-failure message produced by locator resolution,
`extends:` resolution, or predefined-role discovery that refers to one of
the eight packaged roles MUST use the term "predefined role"; it MUST NOT
use "generic role" to mean one of the eight packaged roles. Messages that
refer to the abstract `base -> agent` tree itself MAY continue to use
"generic agent" — that usage is unaffected by this rename.

#### Scenario: A predefined-role load failure uses the new term

- GIVEN a predefined role fails to load due to a malformed manifest
- WHEN the resulting error message is rendered
- THEN it refers to the role as a "predefined role", not a "generic role"

#### Scenario: A reference to the base->agent tree is unaffected

- GIVEN a message referring to `platform/roles/agent` itself (the abstract tree every predefined role extends)
- WHEN it is rendered
- THEN it MAY still call that tree the "generic agent" — this requirement does not rename that usage
