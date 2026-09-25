# Design: Permission class hierarchy replacing prefix-string invariants

## Technical Approach

Hybrid, per the proposal/explore: strings stay canonical at manifests/YAML/logs
(`Requirement: Wire format stability`); a new leaf package,
`src/agents_system/permissions/`, owns the `Permission` class hierarchy and a
`PermissionRegistry` resolving name ↔ class. `Tier` itself is **not** moved —
it stays defined at `harness/registry.py:9-33`, because `__init__.py:60-63`
(the `_EXPORTS` dict entry `"Tier": ("agents_system.harness.registry", "Tier")`)
and `tests/test_public_api.py:29` fix that import path as public API; moving
it would be an unforced breaking change outside this proposal's scope.
`permissions/` imports `Tier` from `harness.registry` (one direction only).

## Module Layout

```
src/agents_system/permissions/
├── __init__.py            # re-exports: Permission, PermissionRegistry, Read,
│                           #   Write, Send, Exec, Run, Spawn, resolve(),
│                           #   register(), the AgentPermissionError tree
├── errors.py               # AgentPermissionError hierarchy (mirrors spec's tree;
│                           #   named to avoid shadowing the built-in PermissionError)
├── base.py                 # Permission root + __init_subclass__ (R1)
├── builtins.py              # 5 action families + resource() factory +
│                            #   registration of all 18 shipped wire names
└── permission_registry.py    # PermissionRegistry: thread-safe resolve/
                             #   register/reverse, package-global instance
```

**Why not put `Permission` in `harness/registry.py` itself?** `ToolSpec.__post_init__`
must call the registry to evaluate R2a/R2b, and the registry needs `Tier`
from `registry.py` — same-module definition would leave the R2a/R2b logic
unable to `import` its own module cleanly for testing/mocking, and would
make `harness/registry.py` (today: pure stdlib, zero internal imports, per
`registry.py:1-6`) the single largest file in the package. A dedicated
package keeps `Tier`'s existing call sites (`loader.py:58`, 8 connector/service
files) untouched and gives downstream extension (spec: "Open hierarchy for
downstream extension") one clear import root.

## Cycle Avoidance

`permissions/base.py` imports `Tier` from `harness.registry` — one direction,
no cycle, since nothing at `registry.py` module-scope needs `permissions`.
The only place `registry.py` needs `permissions` is inside
`ToolSpec.__post_init__` (`registry.py:81-124`) itself, executed at
`ToolSpec()` construction time, long after both modules are fully imported.
This is resolved with a **deferred import inside the method body**:

```python
def __post_init__(self) -> None:
    from agents_system.permissions import evaluate_tool_spec  # deferred

    evaluate_tool_spec(self.name, self.tier, self.required_permissions)
```

This is not a novel pattern for this codebase: `harness/injector.py:20-21`'s
`_emit_async` already does `from agents_system.audit import recorder` inside
the function body for the identical reason (breaking a would-be import
cycle between a leaf-adjacent concern and its consumer). Verified against
the current file: `injector.py:20` reads
`from agents_system.audit import recorder` and `injector.py:21` reads
`from agents_system.audit.sink import AuditSink`, both inside the `try:`
block of `_emit_async`.

## Class API

```python
# permissions/base.py
class Permission:
    tier: ClassVar[Tier]  # unset on Permission itself — root has no tier

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls is Permission:
            return
        own_tier = cls.__dict__.get("tier")  # OWN attr, not inherited
        parent_tier = getattr(cls.__mro__[1], "tier", None)
        if own_tier is None:
            if parent_tier is None:
                raise InvalidPermissionTierError(cls, reason="no tier declared")
            cls.tier = parent_tier  # inherit (spec scenario)
            return
        if parent_tier is not None and _RANK[own_tier] < _RANK[parent_tier]:
            raise InvalidPermissionTierError(cls, own_tier, parent_tier)
```

`cls.__dict__.get("tier")` (not `getattr`) distinguishes "this class declares
its own tier" from "it would inherit one" — this is what makes the spec's
"Subclass omitting a tier override inherits its parent's tier" scenario a
plain inheritance, not a violation, while still catching an *explicit* lower
tier (R1's actual de-escalation check).

`__init_subclass__` never auto-registers — spec: "there is no auto-discovery".
Registration is explicit, via `permission_registry.register(cls, name)`.

## Built-ins Without Hand-Writing Hundreds of Classes

`builtins.py` defines the five action roots (`Read`, `Write`, `Send`, `Exec`,
`Run`, tiers per spec's table) plus one factory:

```python
def resource(
    parent: type[Permission], wire_name: str, *, tier: Tier | None = None
) -> type[Permission]:
    """Create + register a resource-scoped subclass for one wire name."""
    cls = type(
        _class_name(wire_name), (parent,), {} if tier is None else {"tier": tier}
    )
    permission_registry.register(cls, wire_name)
    return cls
```

then a flat registration table drives every shipped wire name — one call per
row, not one hand-written class per row.

## Full Wire-Name Registration Gap (new finding — corrects the spec's scope)

The spec's R2a/R2b compatibility table covers only the 12 `ToolSpec`-backed
cases. The registry's "every wire name usable by a manifest… MUST be
explicitly registered" requirement is broader: `platform/roles/*/manifest.md`
(10 files, confirmed by direct read) declares **18 distinct wire names**,
6 of which back no `ToolSpec` today:

| Wire name | Manifest | Tier assigned |
|---|---|---|
| `read:session` | `base`, `agent` | Read (T0) |
| `read:price_lists` | `sales-agent:16` | Read (T0) |
| `write:session` | `orchestrator:12` | Write (T2) |
| `write:summary_output` | `summary-agent:15` | Write (T2) |
| `spawn:sales-agent`, `spawn:data-agent`, `spawn:summary-agent` | `orchestrator:13-15` | **new** `Spawn` (T2) |

`spawn:*` has **no current prefix classification at all** (not `exec:`/
`write:`/`send:`/`run:` — `registry.py:42-44`'s prefix constants don't cover
it). Under the new mandatory-registration rule, `orchestrator`'s manifest
fails to load unless these three resolve. Design decision: register a new
`Spawn(Permission)` family at `Tier.T2`, exactly matching the spec's own
worked example (`spec.md`, "Declaring a new top-level action" scenario).
This is a genuinely new classification, not a preserved one — current
behavior is *no* classification — so it needs explicit owner confirmation
before PR1 (Open Risk 2).

## PermissionRegistry

One package-global instance in `permission_registry.py`; two dicts
(`_by_name: dict[str, type[Permission]]`, `_by_class: dict[type[Permission], str]`)
guarded by one `threading.Lock` taken on both read and write paths — call
volume is registration-time-bounded (once at import, occasionally at
downstream extension), not a per-request hot path, so lock-free reads are
not worth the complexity. `resolve(name)`, `reverse(cls)`, `register(cls,
name)` (idempotent on exact re-registration, per spec).

**Test isolation.** A `conftest.py` fixture, `reset_permission_registry`
(autouse in `tests/test_permissions_*.py` only, not global — most tests
never touch the registry), snapshots `_by_name`/`_by_class` before each test
via a test-only `permission_registry._snapshot()` / `_restore(snapshot)`
pair (leading-underscore, documented as test-only) and restores after, so a
test registering `ExportWrite` under `"write:export"` cannot leak into the
next test's collision checks.

## Grant Source at Boot

Two separate mechanisms for two separate consumers, per the proposal's own
scope split:

1. **This concrete app (`main.py`).** `main.py:357-364` currently calls
   `build_runtime(role_type=role, registry=registry,
   granted_permissions=definition.permissions, client=deployment,
   roots=roots, session_provider=session_provider)` (AD-5, to remove — the
   `definition.permissions` grant is confirmed on `main.py:360` by direct
   read). Replace with a new `Settings` field, `deploy_grants:
   dict[str, tuple[str, ...]]`, parsed from a `DEPLOY_GRANTS` env var as a
   JSON object (`{"whatsapp__sales-agent": ["read:catalog",
   "write:orders", ...]}`), using `pydantic-settings`' existing JSON-env
   decoding (the same mechanism already used for other structured
   `Settings` fields). **Justification**: `main.py` already resolves all
   runtime config through `Settings` (`config.py:18`, env + `.env`) — a
   second config surface (e.g. a Python file under `deployments/`) would
   fragment config across two loaders for one boot path, and
   `deployments/README.md`'s own layout (`manifest.md`, `policy.md`,
   `role.md`, `skills/` — all markdown, confirmed by direct read, no
   Python entry point described anywhere in that file) supports treating
   the tree as declarative-only rather than adding a Python grant source
   there. Boot fails loudly (`DefinitionError`, matching the existing
   `whatsapp_runtime_id` failure style around `main.py:433-446`) when a
   required `model_id` has no entry.
2. **Library usage** (`__init__.py:15-37`'s documented flow). Unchanged
   signature shape: `build_runtime(..., granted_permissions=[...])` already
   takes the grant directly from caller code — the proposal's "Library usage
   is `deploy(..., grant=[...])`" is satisfied by this existing parameter
   (a convenience `deploy()` wrapper is optional polish, not required).

## EquippedRuntime Grant Ceiling (issue #38)

```python
@dataclasses.dataclass(frozen=True)
class EquippedRuntime:
    ...  # unchanged fields
    deploy_grant_ceiling: frozenset[type[Permission]] = frozenset()  # NEW
```

`build_runtime` (`factory.py:204-271`) resolves `granted_permissions` through
the registry once (`{permission_registry.resolve(p) if isinstance(p, str) else p for p in granted}`)
and stores it as `deploy_grant_ceiling`, in addition to the existing
`granted_tools` computation (unchanged — injector logic stays string/class
resolved the same way as today's set-subset check, now via R3 `issubclass`+
tier). `interceptor.intercept` (`interceptor.py:140-141`, confirmed by
direct read: `effective = set(current_permissions)` /
`if not set(spec.required_permissions) <= effective:`) and
`AgentRuntime.run_turn`'s default (`agent/graph.py:457-459`, confirmed by
direct read: `effective_permissions = (permissions if permissions is not
None else self.permissions)`, currently `self.permissions` = the full role
tuple) both change to intersect against `equipped.deploy_grant_ceiling`
instead — this is the exact fix: Layer-2 revalidation can no longer widen
back out to the role's full permission set.

## Migration of Every Prefix Check

| Site | Old | New |
|---|---|---|
| `registry.py:81-124` `ToolSpec.__post_init__` | 3 `startswith` branches on `_EXEC_PREFIX`/`_WRITE_SEND_PREFIXES`/`_RUN_PREFIX` (constants at `registry.py:42-44`) | R2a+R2b via `permissions.evaluate_tool_spec()`, deferred import |
| `injector.py:77-99` `_deny_reason` | `set(required) <= effective` (string subset) | `issubclass(required_cls, granted_cls) and required_cls.tier <= granted_cls.tier` per R3; T3 untrusted branch (line 87-95) unchanged — already tier-based |
| `loader.py:1501` `_is_exec_permission` | `permission.strip().lower().startswith("exec:")` | removed; replaced by class resolution |
| `loader.py:1560-1585` `_validate_untrusted_input_exec` | filters `permissions` by `_is_exec_permission` | resolves every name in `permissions` to a class, rejects if any `.tier is Tier.T3`, called from the same two sites (`loader.py:1701-1703` merge branch, `:1858-1860` no-override branch) |
| `loader.py:366` `_COMMAND_TOOL_MIN_TIER` | `(Tier.T2, Tier.T3)` | narrowed to `(Tier.T2,)` per Resolved Decision 3 — no T3 `run:` variant ships |

## Sequence Diagrams

**Load** (`loader.resolve()`):
```
resolved_perms: list[str]
  -> for name in resolved_perms: permission_registry.resolve(name) -> cls
  -> if untrusted_input and any(cls.tier is T3): raise UntrustedInputGrantError
  -> AgentDefinition.permissions stays tuple[str, ...]  (wire format unchanged)
```

**Equip** (`build_runtime`):
```
Settings.deploy_grants[model_id]  (JSON env)  ->  granted: Iterable[str | type[Permission]]
  -> ceiling = {resolve(p) if str else p for p in granted}
  -> resolve_tool_surface / resolve_command_tool_surface  (R3 issubclass+tier via _deny_reason)
  -> EquippedRuntime(tools=granted_tools, deploy_grant_ceiling=frozenset(ceiling))
```

**Call-time** (`_execute_tools` -> `intercept`):
```
intercept(tool_name, ..., current_permissions=turn_permissions)
  -> effective = equipped.deploy_grant_ceiling ∩ {resolve(p) for p in current_permissions}
  -> require: spec.required_permissions all covered by effective (R3)
  -> PolicyViolation("permission_revoked") on miss, else execute
```

## Testing Strategy Per PR (Strict TDD; RED-GREEN-REFACTOR)

`openspec/config.yaml`'s `rules.apply.tdd` is `false` repo-wide today; this
change follows Strict TDD by convention for its own new/rewritten tests
regardless (per `AGENTS.md`'s SDD-flow mapping to Strict TDD), without
flipping the repo-wide flag.

| PR | Scope | Tests-first | Est. lines |
|---|---|---|---|
| PR1 | `permissions/` package (base, errors, builtins, registry) | new `test_permissions_base.py` (R1: equal/escalate/de-escalate/missing-tier), `test_permissions_registry.py` (resolve/reverse/collision/idempotent re-register/thread-safety via `ThreadPoolExecutor`), `test_permissions_builtins.py` (all 18 wire names resolve to distinct classes, none is the bare action root) | ~350 |
| PR2 | `ToolSpec` R2a/R2b + injector R3/R4 | rewrite `test_capability_tiers.py`, `test_harness_injector.py`, `test_untrusted_input_invariant.py` to class/tier equivalents; new R2b cases (`read_file` RED before its `ReadFiles(Read, tier=T3)` fix — see Open Risk 1); new R3 descendant-tier cases from spec | ~390 |
| PR3 | Grants + Layer-2 ceiling + `main.py` + `Settings.deploy_grants` | `test_main.py` (boot fails loudly without a grant entry), `test_harness_interceptor.py`, new `test_issue_38_regression.py` (narrower deploy grant denies at Layer-2 despite wider role permissions) | ~340 |
| PR4 | Docs + ADR-003 | none (docs only) | ~150 |

**Review Workload Guard** (`sdd-phase-common.md` Section E): PR2 and PR3 stay
near but under 400 lines individually; PR1 is close at ~350 with tests
dominating line count. `Decision needed before apply: Yes` (delivery
strategy not yet cached this session) — `Chained PRs recommended: Yes`
(matches the proposal's existing 4-PR split) — `400-line budget risk:
Medium` (PR2 and PR1 both close to the ceiling; a scope slip on either
crosses it).

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file
classification, or process-integration boundary. (`use_term`/`exec:command`
already exists and is unchanged by this design; only its *classification
mechanism* moves from prefix to class/tier.)

## Migration / Rollout

No data migration, no persisted-state schema change — matches the
proposal's Rollback Plan. `Settings.deploy_grants` is new-required-if-used
config: any deployment relying on AD-5's auto-grant must add a
`DEPLOY_GRANTS` entry before PR3 ships, or boot fails loudly by design.

## Resolved Decisions

Owner approved all five decisions below on 2026-09-24, prior to the tasks
phase. Nothing in this section remains open; each decision names the PR
that implements it, and PR2/PR3 tasks in `tasks.md` are written against
these exact resolutions, not against the discarded alternatives.

1. **`read:files` resolves to `ReadFiles(Read)` at escalated tier T3.**
   (`operator.py:838-841`, T3 tool requiring only `read:files` → `Read`,
   T0 — confirmed by direct read: `required_permissions=("read:files",)`
   at line 841.) `ReadFiles(Read)` declares an *escalated* tier override
   `tier = Tier.T3` (a valid R1 escalation — host filesystem access is
   genuinely T3-dangerous, unlike catalog/knowledge-base reads) — this
   satisfies R2a (3>=3) and R2b (max required tier 3>=3) with **zero
   manifest edits**, since `operator-agent`'s manifest still just says
   `read:files`; only the class that wire name resolves to changes.
   **PR2 implements this.**
2. **A new `Spawn` action at tier T2 classifies the orchestrator's
   `spawn:*` permissions.** `spawn:sales-agent`, `spawn:data-agent`,
   `spawn:summary-agent` (`orchestrator` manifest, `platform/roles/orchestrator/manifest.md:13-15`)
   currently have zero prefix-based classification — `registry.py:42-44`'s
   prefix constants don't cover `spawn:`. This is a genuinely new
   classification (current behavior: no enforcement at all), owner-approved
   rather than silently adopted, and matches the spec's own "Declaring a
   new top-level action" worked example. **PR1 implements this** (the
   `Spawn(Permission)` class and its three resource-scoped registrations
   ship alongside the other four built-ins).
3. **Declarative command tools are limited to T2 only.** `_COMMAND_TOOL_MIN_TIER`
   (`loader.py:366`) narrows from `(Tier.T2, Tier.T3)` to `(Tier.T2,)`.
   No T3 `run:` variant ships — the alternative considered (a T3-floor
   `run:` permission family) is explicitly rejected; declarative command
   tools stay capped at T2 for this change. Since no shipped
   `platform/roles/*/manifest.md` declares `command_tools:` today, this is
   a pure tightening with no live migration. **PR2 implements this**
   (narrows the loader constant; `Run` stays a single T2 family with no
   T3-floor sibling).
4. **Evals' implicit full-grant default is OUT of scope for this change.**
   (`evals/schema.py:56-65`, `evals/runner.py:347-383` — a second
   AD-5-shaped auto-grant the proposal's Affected Areas table never listed
   as modified.) This change does not touch eval boot/grant behavior.
   **Follow-up:** file a separate GitHub issue tracking this as its own
   AD-5-shaped auto-grant, scoped independently of `permission-model`; not
   filed as part of this SDD change.
5. **`DEPLOY_GRANTS` is the grant source for `main.py` boot; library usage
   is `deploy(..., grant=[...])`.** `Settings.deploy_grants:
   dict[str, tuple[str, ...]]` is parsed from a `DEPLOY_GRANTS` environment
   variable as a JSON object mapping runtime id to a list of wire-name
   strings (e.g. `{"whatsapp__sales-agent": ["read:catalog",
   "write:orders"]}`), via `pydantic-settings`' existing JSON-env decoding
   (`config.py:18`'s `Settings` mechanism — no second config surface).
   Boot fails loudly (`DefinitionError`) when a required `model_id` has no
   `DEPLOY_GRANTS` entry, matching the existing `whatsapp_runtime_id`
   failure style (`main.py:433-446`). Library usage keeps
   `build_runtime(..., granted_permissions=[...])`'s existing signature —
   satisfying `deploy(..., grant=[...])` without a new wrapper being
   required. **PR3 implements this** (`Settings.deploy_grants`, `main.py`'s
   `DEPLOY_GRANTS` read replacing the AD-5 auto-grant, and the
   `EquippedRuntime.deploy_grant_ceiling` it feeds).

## Verification Note (design phase)

Every `file:line` citation above was spot-checked against the current
working tree during this design phase (direct `Read`/`Grep`, not
`codegraph_explore`, since the orchestrating session had already done the
codegraph-backed exploration this design phase reused). One citation error
from the original draft was corrected: `__init__.py:15` (claimed location of
the `_EXPORTS["Tier"]` entry) does not match the file — the `_EXPORTS` dict
literal starts at `__init__.py:60`, and the `"Tier"` entry is at line 63.
Corrected throughout this document to `__init__.py:60-63`. All other cited
line ranges (`registry.py:9-33,42-44,81-124`; `injector.py:20-21,77-99`;
`loader.py:58,366,1501,1560-1585,1701-1703,1858-1860`; `factory.py:204-271`;
`main.py:357-364,433-446`; `config.py:18`; `interceptor.py:140-141`;
`graph.py:457-459`; `command_tools.py:237-239`; `operator.py:838-841`;
`tests/test_public_api.py:29`) matched the file contents exactly or within
the expected tolerance of a multi-line statement/block citation.
