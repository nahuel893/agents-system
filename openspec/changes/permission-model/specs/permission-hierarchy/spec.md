# Permission Hierarchy Specification

## Purpose

Define the class-based `Permission`/`Tier` model that replaces string-prefix
inference (`exec:`, `write:`, `send:`, `run:`) as the source of safety truth.
Danger MUST be declared per class and enforced structurally (class identity +
declared tier), never inferred by parsing a wire string. String wire names
(manifests, YAML, logs, audit) are unchanged; a `PermissionRegistry` is the
only place a string is resolved to a class.

`Tier` is the existing four-level ordinal scale, unchanged: `T0 < T1 < T2 < T3`
(T0 = inherent, T1 = scoped read, T2 = scoped write/send — always revalidated,
T3 = host execution — `operator-agent` branch only).

## Requirements

### Requirement: Permission base class carries an explicit tier

Every concrete `Permission` subclass MUST declare a `tier: Tier` class
attribute at class-definition time. The abstract `Permission` root MUST NOT
be directly usable as a required or granted permission — it carries no tier
and exists only as the hierarchy root.

#### Scenario: Concrete subclass declares a tier

- GIVEN a new `Permission` subclass is defined
- WHEN it declares `tier = Tier.T1`
- THEN the class is valid and eligible for registration

#### Scenario: Concrete subclass omits a tier

- GIVEN a new `Permission` subclass is defined without a `tier` attribute
- WHEN the class body is evaluated
- THEN class creation MUST fail with `InvalidPermissionTierError`

### Requirement: Built-in action classes and their tiers

The system MUST ship five built-in `Permission` subclasses — `Read` (T0),
`Write` (T2), `Send` (T2), `Exec` (T3), `Run` (T2) — with tiers chosen so
every currently shipped `ToolSpec` and every `platform/roles/*/manifest.md`
permission continues to validate under R2a without modification. These tiers
are derived from the current prefix floors: `exec:` requires tool tier T3
exactly; `write:`/`send:` require tool tier T2 or T3; `run:` requires tool
tier T2 or T3; `read:` has no current floor (observed at both T1 and T3).
R2a's tiers are unaffected by R2b (the floor added by this amendment);
one pre-existing tool, `read_file`, passes R2a here but fails R2b — see
Requirement "R2a/R2b compatibility of every currently shipped ToolSpec".

| Class | Tier | Existing evidence |
|---|---|---|
| `Read` | T0 | `read:catalog`, `read:client_registry`, `read:knowledge_base`, `read:conversation_logs`, `read:reports` all run at T1; `read:files` runs at T3 — all satisfy `tool.tier >= T0` |
| `Write` | T2 | `write:orders`/`write:order_items` (`order_writer`, T2) |
| `Send` | T2 | `send:message` (`message_sender`, T2), `send:escalation` (`escalation_notifier`, T2) |
| `Exec` | T3 | `exec:command` (`use_term`, T3) |
| `Run` | T2 | Declarative `command_tools:` (`run:*`), currently unshipped but validated at T2/T3 today |

#### Scenario: Every built-in tier matches an existing tool under R2a

- GIVEN the five built-in tiers above
- WHEN each currently shipped `ToolSpec` is re-validated under R2a (Requirement "ToolSpec permission tier ceiling (R2a)")
- THEN every one (`catalog_search`, `order_writer`, `message_sender`, `escalation_notifier`, `use_term`, `read_file`, `run_report`) MUST still construct without error

#### Scenario: read_file is the sole exception once R2b is also checked

- GIVEN the same tools re-validated under R2b (Requirement "ToolSpec permission tier floor (R2b)")
- WHEN `read_file`'s only required permission, `Read` (T0), is compared against its own tier (T3)
- THEN `read_file` fails R2b while every other tool in this list still passes both R2a and R2b — the full accounting is in Requirement "R2a/R2b compatibility of every currently shipped ToolSpec"

### Requirement: Unique wire name per class; explicit resource-level registration

Each `Permission` subclass registered in the `PermissionRegistry` MUST have
exactly one wire name, and each wire name MUST resolve to exactly one class.
A resource-scoped wire string such as `read:catalog` MUST resolve to a
specific registered subclass of its action family (e.g. a dedicated "catalog
read" class), never to the bare generic action class (`Read`). The generic
action classes exist as hierarchy roots for subclassing and for R3-covering
grants; they MAY optionally be registered under their own canonical name
(e.g. `"read"`), independent of any resource-scoped name. The registry MUST
NOT infer a class from string structure — no prefix, substring, or pattern
parsing of the wire name is permitted anywhere in the resolution path.

#### Scenario: Resource string maps to its own class, not the generic action

- GIVEN `read:catalog` and `read:client_registry` are both registered
- WHEN each name is resolved
- THEN they MUST resolve to two distinct classes, neither of which is the bare `Read` class

#### Scenario: Subclass omitting a tier override inherits its parent's tier

- GIVEN a resource-scoped subclass of `Read` declares no `tier` override
- WHEN the class is created
- THEN its tier equals `Read.tier` (T0), which satisfies R1 (equal tiers are permitted)

#### Scenario: Reusing a shipped name for a different class collides

- GIVEN `"read:catalog"` is already registered to its shipped class
- WHEN a downstream package registers a different class under `"read:catalog"`
- THEN registration MUST raise `PermissionRegistrationCollisionError` (Requirement "Name/class uniqueness and collision")

### Requirement: Open hierarchy for downstream extension

A downstream package MUST be able to (a) subclass a built-in action class to
add a new resource-scoped permission, or (b) subclass `Permission` directly
to declare an entirely new top-level action family, without modifying
`agents_system` source. Both forms MUST be registered explicitly before use;
there is no auto-discovery.

#### Scenario: Subclassing a built-in action

- GIVEN a downstream package needs a new writable resource
- WHEN it defines `class ExportWrite(Write): tier = Tier.T2` and registers it under `"write:export"`
- THEN the class resolves like any built-in permission and participates in R1-R4 unchanged

#### Scenario: Declaring a new top-level action

- GIVEN the orchestrator role already declares `spawn:sales-agent`, `spawn:data-agent`, `spawn:summary-agent` with no backing `ToolSpec` today
- WHEN a future package defines `class Spawn(Permission): tier = Tier.T2` and registers `"spawn:sales-agent"` etc. as its subclasses
- THEN the new family enforces R1-R4 exactly like the five built-ins, with no change to `Permission` or the registry

### Requirement: Subclass tier monotonicity (R1)

A `Permission` subclass's declared tier MUST be greater than or equal to its
parent's declared tier. This MUST be enforced at class-definition time (e.g.
`__init_subclass__`), not deferred to first use, registration, or grant time.

#### Scenario: Equal-tier subclass is valid

- GIVEN `Read` has tier T0
- WHEN a subclass declares `tier = Tier.T0`
- THEN the class is created successfully

#### Scenario: Escalating subclass is valid

- GIVEN `Write` has tier T2
- WHEN a subclass declares `tier = Tier.T3`
- THEN the class is created successfully

#### Scenario: De-escalating subclass is rejected

- GIVEN `Write` has tier T2
- WHEN a subclass declares `tier = Tier.T1`
- THEN class creation MUST raise `InvalidPermissionTierError` before the class object exists

### Requirement: ToolSpec permission tier ceiling (R2a)

A `ToolSpec` of capability tier `t` MUST be permitted to declare a required
permission `p` only when the tool's tier is at least as high as `p`'s
declared tier: **`t >= p.tier`** (equivalently `p.tier <= t`). `ToolSpec`
construction MUST reject any required permission whose tier exceeds the
tool's own tier. This is the same direction as the original R2 in this
spec; the requirement is renamed R2a here only to make room for its
companion floor predicate, R2b, below. No behavior described by this
requirement changes.

Binding clarification, not a reopened decision: an earlier phrasing —
"a tool may require a permission only if the permission's tier is greater
than or equal to the tool's tier" (`p.tier >= t`) — is the inverse of what
the current prefix invariants (`ToolSpec.__post_init__`) implement, and
would reject valid existing tools (e.g. a T3 tool requiring the T2 `Write`
permission). This requirement fixes the direction to `t >= p.tier`,
preserving the owner-approved tier assignments and all existing tool
behavior, per the proposal's own R2-ambiguity risk mitigation.

#### Scenario: T3 tool may require its exact-tier permission

- GIVEN `use_term` has tier T3 and requires `Exec` (T3)
- WHEN the predicate `t >= p.tier` is evaluated (3 >= 3)
- THEN construction succeeds

#### Scenario: T2 tool may require its exact-tier permission

- GIVEN `order_writer` has tier T2 and requires `Write` (T2)
- WHEN the predicate is evaluated (2 >= 2)
- THEN construction succeeds

#### Scenario: Higher-tier tool may require a lower-tier permission

- GIVEN `read_file` has tier T3 and requires `Read` (T0)
- WHEN the predicate is evaluated (3 >= 0)
- THEN construction succeeds under R2a — a T3 tool may still require a lower-floor permission. (This same tool fails the floor predicate R2b below; R2a and R2b are independent, both checked, and both MUST pass.)

#### Scenario: Lower-tier tool requiring a higher-tier permission is rejected

- GIVEN a hypothetical tool has tier T1 and requires `Exec` (T3)
- WHEN the predicate is evaluated (1 >= 3 is false)
- THEN construction MUST raise `PermissionTierMismatchError`

#### Scenario: Regression guard against the inverted predicate

- GIVEN `use_term` has tier T3 and requires `Write` (T2)
- WHEN the correct predicate is evaluated (3 >= 2, true — construction succeeds)
- THEN an implementation MUST NOT instead evaluate the inverted `p.tier >= t` (2 >= 3, false), which would wrongly reject this valid tool

### Requirement: ToolSpec permission tier floor (R2b)

R2a alone lets a T2 or T3 tool declare only cheap, low-tier required
permissions while still performing a dangerous action — the ceiling bounds
how high a required permission's tier may go, but nothing bounds how low.
A `ToolSpec` whose own tier `t` is T2 or T3 MUST require at least one
permission `p` from `required_permissions` whose declared tier is greater
than or equal to the tool's own tier: **`max(p.tier for p in required) >= t`**
for `t` in `{T2, T3}`. A `ToolSpec` whose tier is T0 or T1 has no floor
requirement under R2b — including a T0/T1 tool with an empty
`required_permissions`. `ToolSpec` construction MUST reject a T2 or T3 tool
whose required permissions all have a tier below the tool's own tier, even
when every individual permission already satisfies R2a.

R2a and R2b are independent predicates, both evaluated at `ToolSpec`
construction, and both MUST pass; failing either MUST reject construction.
A tool can satisfy R2a's ceiling on every required permission and still
fail R2b's floor if none of those permissions reaches its own tier — this
is exactly the gap R2b closes: a T3 tool could otherwise require only a T0
`Read` permission and pass R2a alone, hiding a T3-tier action behind a
cheap grant.

#### Scenario: T3 tool requiring only a low-tier permission fails the floor

- GIVEN a T3 tool requires only `read:catalog` (`Read`, T0)
- WHEN the floor predicate is evaluated (`max(0) >= 3` is false)
- THEN construction MUST raise `PermissionFloorViolationError`, even though R2a alone would have passed (`0 <= 3`)

#### Scenario: T1 tool has no floor requirement

- GIVEN a T1 tool requires only `read:catalog` (`Read`, T0)
- WHEN R2b is evaluated for a T1 tool
- THEN R2b imposes no constraint and construction is unaffected by the floor — `Tier.T1` is below the `{T2, T3}` floor-applicable set

#### Scenario: T2 tool with a mix of low- and matching-tier permissions passes

- GIVEN a T2 tool requires `read:x` (`Read`, T0) and `write:y` (`Write`, T2)
- WHEN the floor predicate is evaluated (`max(0, 2) >= 2` is true)
- THEN construction succeeds — at least one required permission reaches the tool's own tier

#### Scenario: T0 tool with no required permissions has no floor requirement

- GIVEN a T0 tool declares `required_permissions=()`
- WHEN R2b is evaluated
- THEN R2b imposes no constraint — `Tier.T0` is below the floor-applicable set, and an empty requirement set is never itself an R2b violation

#### Scenario: R2a and R2b are checked independently

- GIVEN `order_writer` has tier T2 and requires `Write` (T2) and `Write` (T2) again under a second wire name
- WHEN both predicates are evaluated
- THEN R2a passes for every permission (`2 <= 2`) and R2b passes because the maximum required tier reaches the tool's own tier (`2 >= 2`) — both checks run; passing one never substitutes for the other

### Requirement: Grant coverage of permission descendants (R3)

A grant of permission class `P` MUST be treated as covering a required
permission class `D` when `D` is `P` itself or a subclass of `P`, AND
`D`'s declared tier does not exceed `P`'s declared tier (`D.tier <= P.tier`).
Because R1 guarantees a true subclass's tier is never lower than its
parent's, coverage of an actual descendant in practice requires the tier to
be unchanged along the covered path; a descendant that escalated its tier
requires its own explicit grant.

#### Scenario: Ancestor grant covers a same-tier descendant

- GIVEN `Read` (T0) is granted and a registered `CatalogRead(Read)` also declares tier T0
- WHEN a tool requires `CatalogRead`
- THEN the grant of `Read` covers it (`issubclass` holds and `0 <= 0`)

#### Scenario: Exact-class grant always covers itself

- GIVEN `CatalogRead` is granted directly
- WHEN a tool requires `CatalogRead`
- THEN the grant covers it trivially (`D is P`)

#### Scenario: Ancestor grant does not cover an escalated descendant

- GIVEN `Read` (T0) is granted and a registered `SensitiveExport(Read)` declares tier T2 (a valid R1 escalation)
- WHEN a tool requires `SensitiveExport`
- THEN the grant of `Read` MUST NOT cover it (`2 <= 0` is false) — `SensitiveExport` needs its own explicit grant

#### Scenario: Grant of an unrelated class never covers by tier alone

- GIVEN `Write` (T2) is granted
- WHEN a tool requires `Exec` (T3)
- THEN the grant MUST NOT cover it — `Exec` is not a subclass of `Write`, regardless of tier comparison

### Requirement: untrusted_input roles hold no T3 permission (R4)

An `AgentDefinition` with `untrusted_input = true` MUST NOT resolve to an
effective permission set containing any class whose tier is T3, regardless
of the permission's registered wire name. This MUST be enforced (a) at
role/deployment definition load, rejecting the definition, and (b) again
when a deploy-time grant is equipped for an untrusted_input role, rejecting
the grant — independent of the existing tool-execution-time injector T3
barrier, which remains active as defense in depth.

#### Scenario: Untrusted role with only sub-T3 permissions loads

- GIVEN `sales-agent` (`untrusted_input: true`) declares only `read:*`, `write:orders`, `write:order_items`, `send:message` — all T0-T2
- WHEN the definition is loaded
- THEN load succeeds

#### Scenario: Untrusted role declaring a T3 permission by any name fails at load

- GIVEN a role sets `untrusted_input: true` and lists a permission resolving to any T3 class (e.g. `exec:command`, or a future differently-named T3 class)
- WHEN the definition is loaded
- THEN load MUST raise `UntrustedInputGrantError` naming the offending permission and role

#### Scenario: Untrusted role cannot be equipped with a T3 grant at deploy time

- GIVEN an `untrusted_input: true` role definition that (incorrectly) passed load-time validation
- WHEN a deploy-time grant call attempts to equip it with a T3-tier permission
- THEN the grant call MUST raise `UntrustedInputGrantError` and the runtime MUST NOT equip

#### Scenario: Trusted role may still hold T3 permissions

- GIVEN `operator-agent` (`untrusted_input: false`) declares `exec:command` (T3) and `read:files`
- WHEN the definition is loaded and granted
- THEN load and grant both succeed — R4 constrains only `untrusted_input: true` roles

### Requirement: Explicit registration, no inference from string structure

Every wire name usable by a manifest, YAML declaration, or grant MUST be
explicitly registered to exactly one `Permission` subclass before it can be
resolved. The registry MUST NOT parse, split, or pattern-match a wire name
to derive a class.

#### Scenario: Resolving a registered name succeeds

- GIVEN `"read:catalog"` was registered to its class
- WHEN `resolve("read:catalog")` is called
- THEN the registered class is returned

#### Scenario: Resolving an unregistered name fails loudly

- GIVEN `"read:unregistered_resource"` was never registered
- WHEN `resolve("read:unregistered_resource")` is called
- THEN it MUST raise `UnknownPermissionNameError` naming the exact requested string

### Requirement: Name/class uniqueness and collision

Registering a class under a name already bound to a *different* class MUST
raise `PermissionRegistrationCollisionError` naming both the existing and
the incoming class (and their origin, when known). Re-registering the exact
same class object under the exact same name it already holds MUST be
idempotent — no error, no duplicate entry.

#### Scenario: Idempotent re-registration

- GIVEN a class is already registered under a name
- WHEN the same class is registered again under the same name (e.g. re-import)
- THEN the call succeeds with no error and the registry state is unchanged

#### Scenario: Name collision across different classes

- GIVEN `"write:orders"` is registered to its shipped class
- WHEN a different class attempts to register under `"write:orders"`
- THEN registration MUST raise `PermissionRegistrationCollisionError` identifying both classes

#### Scenario: Class-to-different-name collision

- GIVEN a class is already registered under `"send:message"`
- WHEN the same class object is registered again under a second, different name
- THEN registration MUST raise `PermissionRegistrationCollisionError` — a class holds exactly one canonical name

### Requirement: Lookup by name and by class

The registry MUST support forward resolution (wire name to class) and
reverse resolution (class to its exact registered wire name). Reverse
resolution for a class that was never registered MUST fail loudly, the same
as an unknown forward name.

#### Scenario: Reverse resolution returns the canonical name

- GIVEN a class is registered under `"exec:command"`
- WHEN its canonical name is requested by class
- THEN `"exec:command"` is returned

#### Scenario: Reverse resolution of an unregistered class fails

- GIVEN a `Permission` subclass exists in Python but was never registered
- WHEN its canonical name is requested
- THEN it MUST raise `UnknownPermissionNameError`

### Requirement: Registry thread-safety

Registration and resolution MUST be safe under concurrent access from
multiple threads or async tasks without corrupting internal state, losing a
registration, or returning a partially-registered class.

#### Scenario: Concurrent resolves during request handling

- GIVEN built-ins are registered once at import time
- WHEN many concurrent request-handling tasks call `resolve()` for the same or different names
- THEN every call returns a correct, fully-registered class with no race-induced error

### Requirement: Accepted grant forms

An explicit deploy-time grant call MUST accept an iterable containing
`Permission` subclasses, registered wire-name strings, or a mixture of both,
normalizing every entry through the registry before storing the grant.

#### Scenario: Grant by class

- GIVEN `grant([Write, SendMessage])` is called with classes
- WHEN the grant is equipped
- THEN both classes are stored in the grant ceiling

#### Scenario: Grant by name

- GIVEN `grant(["write:orders", "send:message"])` is called with strings
- WHEN the grant is equipped
- THEN each string is resolved through the registry and the resulting classes are stored identically to the class form

### Requirement: No automatic grant

The system MUST NOT derive a deploy-time grant automatically from a role's
declared permission set. AD-5's auto-grant-of-the-full-role-set behavior in
`main.py` MUST be removed; a grant is equipped only via an explicit call
naming the permission classes or wire names to equip.

#### Scenario: Role permissions alone do not become a grant

- GIVEN a role definition declares five permissions and no explicit grant call is made
- WHEN the application attempts to equip the runtime
- THEN the runtime MUST NOT silently treat the role's declared permissions as the grant

### Requirement: Boot failure without an explicit grant

Application boot MUST fail loudly — not default to an empty grant or a full
role-set grant — when a role that requires one has no explicit grant
configured. The failure MUST name the role/deployment missing the grant.
The exact configuration source for supplying a grant is a design-phase
decision; only this observable boot behavior is required here.

#### Scenario: Boot without any configured grant

- GIVEN a deployment configures no explicit grant for `sales-agent`
- WHEN the application boots
- THEN boot MUST fail with an error naming `sales-agent` as missing its grant, and the application MUST NOT start

#### Scenario: Boot with an explicit grant succeeds

- GIVEN a deployment configures an explicit grant covering exactly `sales-agent`'s required permissions
- WHEN the application boots
- THEN boot succeeds and the runtime is equipped with that grant

### Requirement: Layer-2 revalidates against the persisted deploy grant ceiling (issue #38)

`EquippedRuntime` MUST persist the deploy-time grant ceiling separately from
the role definition's declared permission set. Layer-2 revalidation
(`interceptor.intercept` / `AgentRuntime.run_turn`'s default) MUST check a
sensitive tool's required permissions against the intersection of the
current principal's permissions and the persisted deploy grant ceiling —
never against the role's full declared permission set alone.

#### Scenario: Narrower deploy grant denies at Layer-2 even though the role permits (issue #38 regression)

- GIVEN a role declares `Write` among its permissions, but the deploy-time grant equips only `Read`-family permissions (narrower than the role)
- WHEN a T2 tool requiring `Write` reaches Layer-2 revalidation mid-turn
- THEN Layer-2 MUST deny the tool, because the persisted deploy grant ceiling — not the role's full permission tuple — bounds revalidation

#### Scenario: Grant-covered tool passes Layer-2

- GIVEN the deploy grant ceiling includes `Write` and the current principal's permissions include `Write`
- WHEN a T2 tool requiring `Write` reaches Layer-2 revalidation
- THEN Layer-2 grants it

### Requirement: Wire format stability across manifests, YAML, logs, audit

Manifest frontmatter, `command_tools:` YAML declarations, eval scenario YAML
`granted_permissions`, structured logs, and audit payloads MUST continue to
use string permission names unchanged. The class-based model MUST be an
internal policy representation reached only through registry resolution at
ingress; it MUST NOT require any change to an external string format.

#### Scenario: Every shipped manifest loads unchanged

- GIVEN the ten manifests under `platform/roles/`
- WHEN they are loaded after this change ships
- THEN all ten load successfully with zero manifest edits

#### Scenario: Logs and audit keep string names

- GIVEN a tool is granted or denied
- WHEN the event is logged or an audit record is emitted
- THEN the permission is recorded using its registered wire-name string, never a Python class name or repr

### Requirement: Permission error type hierarchy

The system MUST define exception types rooted at a common
`AgentPermissionError` base (named to avoid shadowing the built-in
`PermissionError`, an `OSError` subclass already caught elsewhere in this
codebase, e.g. `connectors/operator.py`'s process-group cleanup):

```
AgentPermissionError
├── UnknownPermissionNameError        # registry lookup failure (forward or reverse)
├── PermissionRegistrationCollisionError  # name↔class collision
├── InvalidPermissionTierError        # R1 violation at class creation
├── PermissionTierMismatchError       # R2a (ceiling) violation at ToolSpec construction
├── PermissionFloorViolationError     # R2b (floor) violation at ToolSpec construction
└── UntrustedInputGrantError          # R4 violation at load or grant/equip time
```

`PermissionFloorViolationError` is a distinct sibling of
`PermissionTierMismatchError`, not a reuse of it: R2a and R2b are
independent predicates (a tool can fail one and pass the other), so a
caller catching one violation type MUST NOT need to inspect the message
text to know whether a ceiling or a floor check failed.

Every message MUST include: the offending permission's registered wire name
and class name, the tool/spec or role/deployment name involved, and the
actual tier value(s) being compared; collision messages MUST additionally
name both classes involved.

#### Scenario: R2a violation message is actionable

- GIVEN a `ToolSpec` construction raises `PermissionTierMismatchError`
- WHEN the message is rendered
- THEN it MUST include the tool name, the permission's wire name and class, the tool's tier, and the permission's tier

#### Scenario: R2b violation message is actionable

- GIVEN a `ToolSpec` construction raises `PermissionFloorViolationError`
- WHEN the message is rendered
- THEN it MUST include the tool name, the tool's tier, and the wire names, classes, and tiers of every required permission that was evaluated against the floor

#### Scenario: Collision message names both classes

- GIVEN a registration raises `PermissionRegistrationCollisionError`
- WHEN the message is rendered
- THEN it MUST include the wire name, the already-registered class, and the incoming class

### Requirement: R2a/R2b compatibility of every currently shipped ToolSpec

Every `ToolSpec` shipped in `agents_system` today MUST construct without
error under both R2a and R2b as defined above, with one documented
exception. This requirement records that audit as a compatibility contract:
a future change to any listed tool's tier or required permissions MUST
re-run this table, not assume it still holds.

| Tool | File:line | Tier | Required permissions → tier | R2a (ceiling) | R2b (floor) |
|---|---|---|---|---|---|
| `catalog_search` | `services/reference.py:335` | T1 | `read:catalog` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |
| `client_lookup` | `services/reference.py:359` | T1 | `read:client_registry` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |
| `message_sender` | `services/reference.py:453` | T2 | `send:message` → Send (T2) | pass (2>=2) | pass (max 2>=2) |
| `order_writer` | `connectors/order_connector.py:200` | T2 | `write:orders`, `write:order_items` → Write (T2) | pass (2>=2 both) | pass (max 2>=2) |
| `run_report` | `connectors/report_connector.py:192` | T1 | `read:reports` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |
| `knowledge_retrieval` | `connectors/platform_connectors.py:219` | T1 | `read:knowledge_base` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |
| `conversation_summarizer` | `connectors/platform_connectors.py:313` | T1 | `read:conversation_logs` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |
| `escalation_notifier` | `connectors/platform_connectors.py:420` | T2 | `send:escalation` → Send (T2) | pass (2>=2) | pass (max 2>=2) |
| `use_term` | `connectors/operator.py:816` | T3 | `exec:command` → Exec (T3) | pass (3>=3) | pass (max 3>=3) |
| `read_file` | `connectors/operator.py:838` | T3 | `read:files` → Read (T0) | pass (3>=0) | **FAIL** (max 0>=3 is false) |
| `session_state` (eval tool) | `evals/live_registry.py:133` | T0 | none | pass (vacuous) | N/A (T0, no floor) |
| public library example | `__init__.py:19-24` (docs, not a shipped runtime tool) | T1 | `read:catalog` → Read (T0) | pass (1>=0) | N/A (T1, no floor) |

#### Scenario: read_file is the one pre-existing R2b failure

- GIVEN `read_file` (`connectors/operator.py:838`) has tier T3 and requires only `read:files`, which resolves to `Read` (T0)
- WHEN R2b is evaluated under this spec (`max(0) >= 3` is false)
- THEN `read_file` fails R2b — this is a pre-existing shipped tool that R2a alone always passed; the design phase MUST decide how to resolve it (e.g. a dedicated higher-tier `Read` subclass for `read:files`, or an additional required permission) WITHOUT this spec amendment silently changing `read_file`'s code

#### Scenario: The declarative command-tool factory can produce a floor-failing spec

- GIVEN `build_command_tool_spec` (`connectors/command_tools.py:239`) builds a `ToolSpec` whose tier is `declaration.tier` (constrained to T2 or T3 by `_COMMAND_TOOL_MIN_TIER`, `harness/loader.py:366`) and whose sole required permission is `declaration.permission`, which always resolves to `Run` (T2)
- WHEN a command tool is declared with `tier: T3` (currently no `platform/roles/*/manifest.md` declares any `command_tools:`, so no such tool ships today)
- THEN that hypothetical T3 declaration would fail R2b (`max(2) >= 3` is false) at `ToolSpec` construction — the design phase MUST decide whether T3 command tools need their own floor-satisfying permission class, or whether `_COMMAND_TOOL_MIN_TIER` should be narrowed to T2 only
