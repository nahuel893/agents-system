# ADR-003 — Permission hierarchy

**Status:** Accepted · **Date:** 2026-09-25 · **Amends:** ADR-002 C.10, C.11, C.12, and AD-5

## Summary

Permissions are represented internally by an explicit `Permission` class hierarchy and an ordinal `Tier` (`T0` through `T3`); wire names remain strings at manifests, YAML, logs, and audit boundaries. The hierarchy replaces prefix-string inference as the safety authority, requires explicit registration, and preserves a deploy-time grant ceiling for Layer-2 revalidation.

## Context

ADR-002 introduced capability tiers, the `untrusted_input` barrier, declarative command tools, and AD-5's startup auto-grant. The shipped implementation required a stronger classification mechanism than permission spelling: a new action must be able to declare its danger structurally, and grants must not widen to the role's full declared permissions during a turn.

The tier values are unchanged: T0 is inherent access, T1 scoped read, T2 scoped write/send, and T3 host execution. This ADR records shipped behavior rather than the earlier proposed design.

## Decision

### Class model and errors

`Permission` is an abstract root with no tier. Concrete subclasses carry a `Tier` class attribute. The shipped error hierarchy is rooted at `AgentPermissionError` (not `PermissionError`, avoiding the built-in `OSError` subclass) and includes the invalid-tier, registry, R2a/R2b, and untrusted-input failures.

R1 is enforced at class-definition time in `Permission.__init_subclass__`: a child may inherit its parent's tier, retain it, or escalate it, but may not declare a lower tier. All tier comparisons use the explicit ordinal `tier_rank` / `_RANK` mapping rather than raw enum comparisons.

Built-in action families are `Read` (T0), `Write` (T2), `Send` (T2), `Exec` (T3), `Run` (T2), and `Spawn` (T2). Resource-scoped names resolve to dedicated subclasses. `ReadFiles(Read)` explicitly escalates `read:files` to T3.

### Tool and grant enforcement

`ToolSpec.__post_init__` defers its import of `evaluate_tool_spec` until construction. This avoids the `permissions`/`harness` import cycle: `permissions.base` imports `Tier` from `harness.registry`, while `harness.loader` and other harness modules likewise use function-local permission imports following the existing injector cycle-avoidance pattern.

`evaluate_tool_spec` enforces both predicates:

- **R2a, ceiling:** each required permission class must satisfy `tool tier >= permission tier`.
- **R2b, floor:** a T2 or T3 tool must have at least one required permission whose tier reaches the tool's tier. T0 and T1 tools have no floor requirement.

R3 defines grant coverage: a granted class covers a required class only when `issubclass(required, granted)` and the required tier does not exceed the granted tier. A same-tier descendant can therefore be covered by its ancestor; an escalated descendant requires an explicit grant.

R4 prohibits an `untrusted_input` role from holding any T3 permission, regardless of its wire name. Loader validation resolves manifest permissions and rejects a T3 class at definition load with `UntrustedInputGrantError`; `build_runtime` independently rejects a T3 grant for such a role. The injector also retains its T3-tool barrier as defense in depth.

`build_runtime` accepts both registered wire-name and class-form grants, normalizing both to the same `frozenset[type[Permission]]`. Before persisting `EquippedRuntime.deploy_grant_ceiling`, it retains only granted classes that R3-cover a permission declared by the role. Thus a raw grant is not itself the deployed ceiling unless it covers the role's declared surface.

Layer 2 fixes issue #38 by checking sensitive calls against the persisted `deploy_grant_ceiling`, intersected with current permissions, rather than against the role's entire declared set. `AgentRuntime.run_turn` defaults to that persisted ceiling, not `definition.permissions`.

### Registry and wire format

`PermissionRegistry` maps a registered wire name to one permission class and reverses a registered class to its one canonical name. It has no prefix, substring, or other name inference. A single lock guards reads and writes. Its `get_or_register` operation is atomic: it checks for an existing class, creates one only when absent, and registers it within one lock acquisition, avoiding a resolve-then-register race.

Manifest-declared `run:` permissions are registered automatically as `Run` subclasses through that atomic operation. All external permission representations remain string wire names; class resolution is an internal policy boundary.

### Resolved decisions shipped with this model

1. **`ReadFiles` escalation.** `read:files` resolves to `ReadFiles(Read)` at T3, satisfying both R2a and R2b for host filesystem access.
2. **`Spawn` family.** `Spawn` is a new top-level T2 `Permission` family for the orchestrator's `spawn:*` permissions.
3. **Command-tool ceiling.** Declarative `command_tools` are limited to T2 exactly; T3 is no longer accepted.
4. **Evals excluded.** Evals' implicit full-grant default is outside this change.
5. **Explicit boot grants.** `DEPLOY_GRANTS` is the `main.py` deploy-time grant source. Each configured `model_id` requires an entry; boot fails rather than auto-granting `definition.permissions`.

### ADR-002 amendments

This ADR preserves ADR-002's historical text while superseding the following shipped-behavior claims:

- **C.10 — capability tiers:** tier values remain T0–T3, but classification and enforcement move from prefix-string matching to class-based dispatch.
- **C.11 — `untrusted_input`:** the invariant is generalized from `exec:*` spelling to every T3-tier class and raises `UntrustedInputGrantError`.
- **C.12 — declarative `command_tools`:** the allowed tier ceiling is narrowed to T2 only; T3 declarations are rejected.
- **AD-5 auto-grant:** AD-5 is removed. `main.py` no longer grants `definition.permissions`; it requires an explicit `DEPLOY_GRANTS` entry per `model_id` or refuses to boot.

## Open follow-ups

- **Issue #47:** evals retain an implicit full-grant default. Resolved Decision 4 deliberately excludes that behavior from this ADR.
- **Library class-form grants:** `build_runtime(..., granted_permissions=[SomeUnregisteredClass])` does not universally fail at grant time. An unregistered class that R3-covers a role-declared permission can enter `deploy_grant_ceiling`; when `AgentRuntime.run_turn` uses its default permissions, it reverse-resolves that class and raises `UnknownPermissionNameError` before graph execution or a tool call. An unregistered class that covers no declared permission is omitted from the ceiling and does not trigger that deferred failure. Callers that explicitly pass turn permissions avoid the default reverse-resolution path. Registration requirements should be enforced earlier for this library-only case.

## Corrections

- The shipped root error is `AgentPermissionError`, not `PermissionError`.
- Tier ordering and registry get-or-create behavior are explicit and lock-protected, respectively; neither relies on incidental enum ordering or a separate resolve-then-register sequence.
- The persisted grant ceiling is bounded by R3 coverage of the role's declared permissions, not merely normalized from the raw grant list.
