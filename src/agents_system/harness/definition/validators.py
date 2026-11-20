"""Invariant checks on a child definition against its parent: tools, command tools, permissions, autonomy, untrusted input and execution limits."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from agents_system.harness.definition.errors import (
    DefinitionError,
)
from agents_system.harness.definition.model import (
    RawDefinition,
)
from agents_system.harness.definition.policy import (
    _AUTONOMY_RANK,
    _DEPLOYMENT_RULE,
    _PLATFORM_DEFAULT_LIMITS,
)
from agents_system.harness.registry import Tier

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_tools(parent: RawDefinition, override: RawDefinition) -> None:
    parent_set = set(parent.tools)
    override_set = set(override.tools)
    extra = override_set - parent_set
    if extra:
        raise DefinitionError(
            f"Invariant violation — tools: override requests tools not present in "
            f"the parent surface: {sorted(extra)}.  "
            f"Parent tools: {sorted(parent_set)}"
        )


def _validate_command_tools(parent: RawDefinition, override: RawDefinition) -> None:
    """ADR-002 C.12 — a deployment may only REMOVE declared command_tools,
    never add one the role did not already declare. Mirrors `_validate_tools`
    exactly, on the NAME set (`command_tool_declarations` carries the full
    entries, which a deployment never redefines)."""
    parent_set = set(parent.command_tools)
    override_set = set(override.command_tools)
    extra = override_set - parent_set
    if extra:
        raise DefinitionError(
            f"Invariant violation — command_tools: override requests command "
            f"tools not declared by the role: {sorted(extra)}. A deployment "
            f"may only remove declared command_tools, never add new ones "
            f"(ADR-002 C.12). Role command_tools: {sorted(parent_set)}"
        )


def _validate_permissions(
    parent: RawDefinition,
    resolved_perms: list[str],
) -> None:
    parent_perms = (
        set(parent.permissions) if isinstance(parent.permissions, list) else set()
    )
    resolved_set = set(resolved_perms)
    extra = resolved_set - parent_perms
    if extra:
        raise DefinitionError(
            f"Invariant violation — permissions: resolved set contains permissions "
            f"not present in the parent: {sorted(extra)}.  "
            f"Parent permissions: {sorted(parent_perms)}"
        )


def _validate_autonomy(
    ceiling: str,
    level: str,
    *,
    who: str = "override",
    against: str = "parent ceiling",
    rule: str = _DEPLOYMENT_RULE,
) -> None:
    """``level`` may match or tighten ``ceiling``, never exceed it.

    The one autonomy ceiling check, shared by both trust boundaries: a
    deployment override against its resolved role (``_merge_validated``)
    and an importer-authored agent against its parent
    (``_apply_importer_ceiling``). ``who``/``against``/``rule`` only
    phrase the error; the defaults are the deployment wording.
    """
    if not level:
        # Not declared: the child inherits the parent's level, which
        # cannot exceed itself. Nothing to check.
        return
    ceiling_rank = _AUTONOMY_RANK.get(ceiling)
    level_rank = _AUTONOMY_RANK.get(level)

    if ceiling_rank is None:
        raise DefinitionError(
            f"Invariant violation — autonomy: unknown parent autonomy level "
            f"'{ceiling}'.  Valid values: {list(_AUTONOMY_RANK)}"
        )
    if level_rank is None:
        raise DefinitionError(
            f"Invariant violation — autonomy: unknown {who} autonomy level "
            f"'{level}'.  Valid values: {list(_AUTONOMY_RANK)}"
        )
    if level_rank > ceiling_rank:
        raise DefinitionError(
            f"Invariant violation — autonomy: {who} level '{level}' "
            f"(rank {level_rank}) exceeds {against} '{ceiling}' "
            f"(rank {ceiling_rank}).  {rule}"
        )


# ---------------------------------------------------------------------------
# ADR-002 C.11 / permission-model R4 — `untrusted_input` holds no T3 permission
# ---------------------------------------------------------------------------


def _validate_untrusted_input_monotonic(
    parent: RawDefinition, child: RawDefinition
) -> None:
    """Monotonic, once true (ADR-002 C.11).

    Mirrors ``_validate_autonomy``'s rank-comparison pattern for a two-value
    rank (``false=0 < true=1``): once ``parent`` has ``untrusted_input=true``,
    ``child`` may not declare it ``false``. A ``child`` that declares nothing
    (``None``) inherits ``parent``'s value and is never a violation — same
    reasoning as an undeclared ``autonomy`` above.

    Shared by BOTH directions the ADR names ("no descendant or deployment
    override" — the ADR's own wording): ``_fold_parent_into_child`` calls
    this once per level of the ``extends:`` chain (``child`` is the next
    role's own declaration, ``parent`` the chain folded so far), and
    ``_merge_validated`` calls it once for the deployment-override boundary
    (``child`` is the override, ``parent`` the resolved generic role). A
    role-to-role composition and a deployment override are told apart by
    ``child.deployment`` (``None`` for a role, a client name for an
    override) purely to phrase the right error — the rule itself is
    identical either way, because unlike ``autonomy`` this flag is not a
    policy choice a role's own author is trusted to loosen: it is a fact
    about where untrusted input can reach, and a false one is fail-OPEN for
    the exec:* invariant below.
    """
    if child.untrusted_input is None:
        return
    if not (parent.untrusted_input and not child.untrusted_input):
        return
    if child.deployment is not None:
        raise DefinitionError(
            f"Invariant violation — untrusted_input: override for "
            f"'{parent.role_name}'/'{child.deployment}' sets "
            f"untrusted_input=false, but the resolved role already has "
            f"untrusted_input=true.  Deployments may only restrict, not "
            f"elevate — once a role's input can be untrusted, no override "
            f"may set it back to trusted."
        )
    raise DefinitionError(
        f"Invariant violation — untrusted_input: role '{child.role_name}' "
        f"declares untrusted_input=false, but its parent '{parent.role_name}' "
        f"already has untrusted_input=true.  Role-to-role composition may "
        f"only restrict, not elevate trust back — once a role's input can "
        f"be untrusted, no descendant may set it back to trusted."
    )


def _validate_untrusted_input_exec(
    untrusted_input: bool,
    permissions: list[str],
    *,
    role_name: str,
) -> None:
    """`untrusted_input=true` holds no T3-tier permission (R4, generalizing
    ADR-002 C.11's `exec:*` mutual-exclusion from a wire-name prefix match
    to any T3 class, regardless of its registered name).

    The lethal-trifecta guard: a role whose input can come from an untrusted
    source may never also hold host-execution-tier permissions. Checked
    unconditionally by both callers — the ``merge()`` branch and the
    no-override branch of ``resolve()`` — so a role resolved with no
    deployment override enforces this exactly like one that has one.

    Deferred import: see `_ensure_command_tool_permission_registered`'s
    docstring for why a module-level `agents_system.permissions` import in
    this file is a real circular import, not a theoretical one.
    """
    if not untrusted_input:
        return
    from agents_system.permissions import UntrustedInputGrantError
    from agents_system.permissions import resolve as resolve_permission

    for name in sorted(permissions):
        cls = resolve_permission(name)
        if cls.tier is Tier.T3:
            raise UntrustedInputGrantError(name, cls, role_name)


def _shown_limit(value: Any) -> str:
    """``str(value)`` for an error message. Python refuses to print an int
    of more than 4300 digits (``ValueError``), and that must not escape in
    place of the ``DefinitionError`` it was meant to phrase."""
    try:
        return str(value)
    except ValueError:
        return f"<an integer of {value.bit_length()} bits>"


def _validate_execution_limits(
    baseline: Mapping[str, Any],
    override_limits: Mapping[str, Any],
    *,
    who: str = "override",
    against: str = "parent/platform default",
    rule: str = _DEPLOYMENT_RULE,
    defaults: Mapping[str, Any] = _PLATFORM_DEFAULT_LIMITS,
) -> None:
    """Validate that override limits are stricter or equal to baseline.

    For each key present in override_limits, its numeric value must be <= the
    baseline value for that key. If any value is greater (looser), raise
    DefinitionError with a precise message naming the field, the override
    value, and the baseline.

    Shared by the deployment boundary (``_merge_validated``) and the importer
    boundary (``_apply_importer_ceiling``); ``who``/``against``/``rule``
    only phrase the error, defaulting to the deployment wording.

    ``defaults`` stands in for a key neither side sets: the platform
    defaults, or, under a deployment ceiling (#278), ``_limit_defaults``.
    """
    for key, override_value in override_limits.items():
        # A role's `execution_limits` may name only some keys. Falling back to
        # the PLATFORM default for the rest is what keeps a partial dict from
        # becoming an unbounded one: without it, a deployment could raise any
        # limit its role happened not to mention.
        # `dict.get(key, default)` returns the default only when the key is
        # ABSENT — never when its value is None. So the previous version
        # closed the omitted-key half of this and left the null-valued half
        # wide open, which matters because `execution_limits: null` is a
        # shipped idiom in six policy files meaning "no opinion, take the
        # platform defaults". Written per-key it reads identically to an
        # author and silently REMOVED the ceiling instead of applying it.
        baseline_value = baseline.get(key)
        if baseline_value is None:
            baseline_value = defaults.get(key)
        if baseline_value is None:
            # Genuinely unknown to both — a new limit nobody has a ceiling for.
            continue
        # The same null reading on the override side: `graph._effective_limits`
        # runs a null value as the platform default, so that is what gets
        # checked. Otherwise a null would lift a parent's tighter limit back
        # up to the default while comparing as "no value".
        value = override_value
        if value is None:
            value = defaults.get(key)
        # `bool` is an `int` subclass, so without the explicit check `True`
        # passed as a limit of 1.
        if isinstance(value, bool) or not isinstance(value, int | float):
            described = (
                "null, with no platform default to fall back to,"
                if override_value is None
                else f"a {type(override_value).__name__},"
            )
            raise DefinitionError(
                f"Invariant violation — execution_limits: {who} sets {key} to "
                f"{described} which is not a number.  {rule}"
            )
        # Issue #93: `-inf` and negative values compared as "tighter" and
        # were accepted. A limit is a finite count or number of seconds;
        # 0 is still a valid (if drastic) tightening.
        #
        # Issue #106: only a float can be non-finite. `math.isfinite` on an
        # int too large for a float (`10**400`) raised `OverflowError`
        # instead of this error; an int compares exactly with any float.
        if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
            raise DefinitionError(
                f"Invariant violation — execution_limits: {who} sets "
                f"{key}={_shown_limit(override_value)}, which is not a finite, "
                f"non-negative number.  {rule}"
            )
        # `not <=` rather than `>`: NaN compares false both ways, so `>`
        # accepted it, and a NaN limit bounds nothing.
        if not value <= baseline_value:
            shown = (
                f"null (the platform default, {value})"
                if override_value is None
                else _shown_limit(override_value)
            )
            raise DefinitionError(
                f"Invariant violation — execution_limits: {who} sets "
                f"{key}={shown} which exceeds {against} "
                f"{key}={baseline_value}.  {rule}"
            )
