"""YAML scenario schema for the live-eval pipeline (#169, ADR-002 E.18).

A scenario is a role, one or more user input turns, and behavior assertions
-- never an assertion on the model's exact generated text (real models are
not deterministic enough for string-equality checks to be meaningful or
stable; see the module docstring in `runner.py` for how assertions are
evaluated). See `docs/platform/live-eval.md` for the full schema reference
and worked example.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Mapping
from typing import Any

import yaml

from agents_system.permissions import UnknownPermissionNameError
from agents_system.permissions.permission_registry import permission_registry


class ScenarioError(Exception):
    """Raised when a scenario file is structurally invalid."""


#: A scenario whose guardrail must hold in every run it was exercised in
#: (#81) -- a permission boundary, a forbidden action, a non-fabrication
#: obligation. See `runner.evaluate_assertions`'s `exercised` semantics and
#: `runner.ScenarioResult.gate` for how this category is enforced.
CATEGORY_GUARDRAIL = "guardrail"
#: A scenario judged against a configurable minimum success rate (#81),
#: never a strict 100% -- the default case for ordinary capability checks.
CATEGORY_HAPPY_PATH = "happy_path"
_CATEGORIES = (CATEGORY_GUARDRAIL, CATEGORY_HAPPY_PATH)

#: `Scenario.threshold`'s value when a happy-path scenario does not
#: override it (#81's suggested default).
DEFAULT_HAPPY_PATH_THRESHOLD = 0.8

#: #76 -- the subset of `agent.graph._ENFORCED_LIMIT_KEYS` a scenario may
#: override via `Scenario.execution_limits_override`. Kept as this module's
#: own tuple (schema.py has no dependency on agent.graph) so a YAML typo is
#: rejected at LOAD time, before `run_scenario` ever tries to apply it.
_OVERRIDABLE_EXECUTION_LIMIT_KEYS = (
    "max_tool_calls",
    "total_execution_timeout_s",
    "tool_call_timeout_s",
)


@dataclasses.dataclass(frozen=True)
class ScenarioAssertions:
    """Behavior assertions a scenario run is checked against.

    Every field defaults to "assert nothing" (empty tuple / ``None``) so a
    scenario only declares the checks it actually cares about. See
    `runner.evaluate_assertions` for exactly how each field is evaluated
    against a run's transcript.
    """

    #: Tool names that MUST appear among the tools the model called.
    tools_called: tuple[str, ...] = ()
    #: Tool names that MUST NOT appear among the tools the model called.
    tools_not_called: tuple[str, ...] = ()
    #: ``None`` asserts nothing. ``True``/``False`` asserts whether the
    #: Layer-2 interceptor blocked at least one call this run.
    permission_denied: bool | None = None
    #: ``None`` asserts nothing. ``True`` requires a successful
    #: ``escalation_notifier`` call; ``False`` requires it was never called.
    escalation_expected: bool | None = None
    #: #76 -- tool names that MUST have been attempted AND blocked by the
    #: Layer-2 interceptor (the same denial `permission_denied` checks, but
    #: tied to exactly which tool it hit). Unlike `permission_denied` (any
    #: call, unnamed), this names the specific tool the block must cover.
    tool_blocked: tuple[str, ...] = ()
    #: #76 -- ``None`` asserts nothing. ``True`` requires the turn's
    #: `max_tool_calls` budget was exhausted (the harness's own fixed
    #: `agent.graph._limit_reached` terminal message -- harness-authored
    #: text, not the model's, so matching it is not a text-equality
    #: assertion on model output). ``False`` requires it was never reached.
    limit_reached: bool | None = None
    #: #76 -- audit event types (`audit.events._AuditEventBase.event_type`
    #: values, e.g. ``"runtime_timeout"``) that MUST have been captured by
    #: this run's `AuditSink` at least once.
    audit_event: tuple[str, ...] = ()
    #: #76 -- tool names that MUST have been attempted but must NOT have
    #: succeeded (blocked, timed out, or a connector-reported ``error_kind``
    #: -- any non-success outcome). Unlike `tools_not_called` (never even
    #: attempt it), this allows the attempt and requires it to fail -- the
    #: shape a sandbox/containment denial takes (e.g. `read_file` against an
    #: out-of-root path: the model may call it, the sandbox must refuse it).
    not_executed: tuple[str, ...] = ()
    #: #76 -- explicit override of `AssertionOutcome.exercised`: tool names
    #: whose mere ATTEMPT (regardless of outcome) marks this run exercised.
    #: When non-empty this REPLACES every other field's automatic exercised
    #: inference for this run (see `runner.evaluate_assertions`'s docstring)
    #: -- for a scenario whose real exercise condition is not implied by
    #: `tools_not_called`/`permission_denied`/`escalation_expected` (e.g. a
    #: prompt-injection scenario whose forbidden tool is outside the role's
    #: own surface and so never appears in `called` at all -- the actual
    #: exercise signal there is "was the poisoned data source retrieved").
    guardrail_exercised: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Scenario:
    """One atomic eval task: a role, its input turns, and its assertions."""

    name: str
    role: str
    turns: tuple[str, ...]
    assertions: ScenarioAssertions = dataclasses.field(
        default_factory=ScenarioAssertions
    )
    description: str = ""
    #: Optional deployment client to resolve the role under (see
    #: `harness.loader.resolve`). ``None`` resolves the generic platform role,
    #: which is what every scenario shipped with the library scope uses.
    client: str | None = None
    #: ``None`` selects the runner's named ``all-declared`` compatibility
    #: default: grant every permission the role itself declares
    #: (``definition.permissions``). A scenario that deliberately tests a
    #: permission boundary supplies a strict subset of wire-name strings.
    granted_permissions: tuple[str, ...] | None = None
    #: The file this scenario was loaded from, for error messages and result
    #: reporting. ``None`` for a scenario built directly in code (tests).
    source: pathlib.Path | None = None
    #: #81 -- this scenario's declared class: `CATEGORY_GUARDRAIL` (must
    #: hold in 100% of exercised runs) or `CATEGORY_HAPPY_PATH` (judged
    #: against `threshold`). Defaults to `CATEGORY_HAPPY_PATH` so every
    #: scenario file predating #81 keeps its previous (ungated) shape
    #: unless explicitly reclassified.
    category: str = CATEGORY_HAPPY_PATH
    #: Happy-path-only override of `runner.DEFAULT_HAPPY_PATH_THRESHOLD`.
    #: ``None`` uses the default. Always paired with `threshold_reason` --
    #: see `load_scenario`'s validation.
    threshold: float | None = None
    #: Why `threshold` overrides the default. Required together with
    #: `threshold`, so a non-default bar is never silently unexplained.
    threshold_reason: str | None = None
    #: #76 -- Layer-2 permissions for this scenario's turn(s), passed as
    #: `AgentRuntime.run_turn_with_usage`'s `permissions=` override. ``None``
    #: (default) passes nothing, so Layer-2 falls back to its own default
    #: (the equipped runtime's deploy grant ceiling) -- unchanged for every
    #: scenario that predates #76. Set this NARROWER than
    #: `granted_permissions` to reach Layer-2 revalidation after Layer-1 has
    #: already equipped the tool: `granted_permissions` alone cannot express
    #: that, since it also controls the deploy-time Layer-1 surface
    #: `build_runtime` equips.
    turn_permissions: tuple[str, ...] | None = None
    #: #76 -- per-scenario override of a subset of the resolved role's
    #: `execution_limits` (`max_tool_calls`, `total_execution_timeout_s`,
    #: `tool_call_timeout_s`), merged over whatever the role's own manifest
    #: declares. ``None`` (default) changes nothing. Exists so a guardrail
    #: scenario can make hitting its own limit deterministic (e.g.
    #: `max_tool_calls: 2` against a task that naturally takes several
    #: calls) instead of depending on the role's production budget
    #: happening to be small enough for a real model to exceed it.
    execution_limits_override: Mapping[str, float] | None = None


def _require_str_tuple(
    value: Any, *, field: str, source: pathlib.Path
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ScenarioError(
            f"{source}: '{field}' must be a list of strings, got {value!r}"
        )
    return tuple(value)


def _optional_bool(value: Any, *, field: str, source: pathlib.Path) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ScenarioError(
            f"{source}: '{field}' must be a boolean (true/false), got {value!r}"
        )
    return value


def _validated_permission_tuple(
    value: Any, *, field: str, source: pathlib.Path
) -> tuple[str, ...] | None:
    """Shared by `granted_permissions` and `turn_permissions` (#76): both are
    an optional wire-name list, each entry checked against the permission
    registry so a typo fails loudly at load time.
    """
    if value is None:
        return None
    names = _require_str_tuple(value, field=field, source=source)
    for name in names:
        try:
            permission_registry.resolve(name)
        except UnknownPermissionNameError:
            raise ScenarioError(
                f"{source}: '{field}' names an unknown permission {name!r}"
            ) from None
    return names


def _optional_execution_limits(
    value: Any, *, field: str, source: pathlib.Path
) -> Mapping[str, float] | None:
    """#76 -- validate `execution_limits_override`: a mapping whose keys are
    a subset of `_OVERRIDABLE_EXECUTION_LIMIT_KEYS` and whose values are
    positive numbers. Rejected at load time, before `run_scenario` ever
    tries to merge it into a resolved role's `execution_limits`.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ScenarioError(f"{source}: '{field}' must be a mapping, got {value!r}")
    unknown = sorted(set(value) - set(_OVERRIDABLE_EXECUTION_LIMIT_KEYS))
    if unknown:
        raise ScenarioError(
            f"{source}: '{field}' names unknown key(s) {unknown!r}; only "
            f"{_OVERRIDABLE_EXECUTION_LIMIT_KEYS!r} may be overridden"
        )
    result: dict[str, float] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ScenarioError(
                f"{source}: '{field}.{key}' must be a number, got {raw!r}"
            )
        if raw <= 0:
            raise ScenarioError(
                f"{source}: '{field}.{key}' must be a positive number, got {raw!r}"
            )
        result[key] = float(raw)
    return result


def load_scenario(path: pathlib.Path) -> Scenario:
    """Parse one YAML scenario file.

    Raises `ScenarioError`, naming *path*, for any structural problem: not a
    mapping, missing/invalid `role`, empty or non-string `turns`, an
    assertion field of the wrong type, an unrecognized `category`, a
    `threshold` given without `threshold_reason` (or vice versa), a
    `threshold` on a `CATEGORY_GUARDRAIL` scenario, or a `threshold` outside
    `(0, 1]`.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML -- {exc}") from exc

    if not isinstance(raw, dict):
        raise ScenarioError(
            f"{path}: a scenario must be a YAML mapping, got {type(raw).__name__}"
        )

    role = raw.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ScenarioError(
            f"{path}: 'role' is required and must be a non-empty string"
        )

    turns = _require_str_tuple(raw.get("turns"), field="turns", source=path)
    if not turns:
        raise ScenarioError(
            f"{path}: 'turns' must be a non-empty list of user messages"
        )

    raw_assertions = raw.get("assertions") or {}
    if not isinstance(raw_assertions, dict):
        raise ScenarioError(f"{path}: 'assertions' must be a mapping")

    assertions = ScenarioAssertions(
        tools_called=_require_str_tuple(
            raw_assertions.get("tools_called"),
            field="assertions.tools_called",
            source=path,
        ),
        tools_not_called=_require_str_tuple(
            raw_assertions.get("tools_not_called"),
            field="assertions.tools_not_called",
            source=path,
        ),
        permission_denied=_optional_bool(
            raw_assertions.get("permission_denied"),
            field="assertions.permission_denied",
            source=path,
        ),
        escalation_expected=_optional_bool(
            raw_assertions.get("escalation_expected"),
            field="assertions.escalation_expected",
            source=path,
        ),
        tool_blocked=_require_str_tuple(
            raw_assertions.get("tool_blocked"),
            field="assertions.tool_blocked",
            source=path,
        ),
        limit_reached=_optional_bool(
            raw_assertions.get("limit_reached"),
            field="assertions.limit_reached",
            source=path,
        ),
        audit_event=_require_str_tuple(
            raw_assertions.get("audit_event"),
            field="assertions.audit_event",
            source=path,
        ),
        not_executed=_require_str_tuple(
            raw_assertions.get("not_executed"),
            field="assertions.not_executed",
            source=path,
        ),
        guardrail_exercised=_require_str_tuple(
            raw_assertions.get("guardrail_exercised"),
            field="assertions.guardrail_exercised",
            source=path,
        ),
    )

    granted_permissions = _validated_permission_tuple(
        raw.get("granted_permissions"), field="granted_permissions", source=path
    )
    turn_permissions = _validated_permission_tuple(
        raw.get("turn_permissions"), field="turn_permissions", source=path
    )
    execution_limits_override = _optional_execution_limits(
        raw.get("execution_limits_override"),
        field="execution_limits_override",
        source=path,
    )

    client = raw.get("client")
    if client is not None and not isinstance(client, str):
        raise ScenarioError(f"{path}: 'client' must be a string, got {client!r}")

    raw_category = raw.get("category")
    if raw_category is None:
        category = CATEGORY_HAPPY_PATH
    elif raw_category not in _CATEGORIES:
        raise ScenarioError(
            f"{path}: 'category' must be one of {_CATEGORIES!r}, got {raw_category!r}"
        )
    else:
        category = raw_category

    raw_threshold = raw.get("threshold")
    raw_threshold_reason = raw.get("threshold_reason")
    if (raw_threshold is None) != (raw_threshold_reason is None):
        raise ScenarioError(
            f"{path}: 'threshold' and 'threshold_reason' must be given "
            "together, or not at all"
        )
    threshold: float | None = None
    threshold_reason: str | None = None
    if raw_threshold is not None:
        if category != CATEGORY_HAPPY_PATH:
            raise ScenarioError(
                f"{path}: 'threshold' only applies to category "
                f"{CATEGORY_HAPPY_PATH!r} scenarios -- a "
                f"{CATEGORY_GUARDRAIL!r} scenario's threshold is always 100%"
            )
        if isinstance(raw_threshold, bool) or not isinstance(
            raw_threshold, (int, float)
        ):
            raise ScenarioError(
                f"{path}: 'threshold' must be a number, got {raw_threshold!r}"
            )
        if not (0.0 < float(raw_threshold) <= 1.0):
            raise ScenarioError(
                f"{path}: 'threshold' must be between 0 (exclusive) and 1 "
                f"(inclusive), got {raw_threshold!r}"
            )
        if (
            not isinstance(raw_threshold_reason, str)
            or not raw_threshold_reason.strip()
        ):
            raise ScenarioError(
                f"{path}: 'threshold_reason' must be a non-empty string"
            )
        threshold = float(raw_threshold)
        threshold_reason = raw_threshold_reason

    return Scenario(
        name=str(raw.get("name") or path.stem),
        role=role,
        turns=turns,
        assertions=assertions,
        description=str(raw.get("description") or ""),
        client=client,
        granted_permissions=granted_permissions,
        source=path,
        category=category,
        threshold=threshold,
        threshold_reason=threshold_reason,
        turn_permissions=turn_permissions,
        execution_limits_override=execution_limits_override,
    )


def load_scenarios(directory: pathlib.Path) -> tuple[Scenario, ...]:
    """Load every ``*.yaml``/``*.yml`` scenario file directly under *directory*,
    sorted by file name for a deterministic run order."""
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    return tuple(load_scenario(path) for path in sorted(paths))
