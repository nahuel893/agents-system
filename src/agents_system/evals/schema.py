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
from typing import Any

import yaml


class ScenarioError(Exception):
    """Raised when a scenario file is structurally invalid."""


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
    #: ``None`` means "grant everything the role itself declares"
    #: (``definition.permissions``) -- the correct default for proving normal
    #: behavior. A scenario that deliberately tests a permission boundary
    #: narrows this to a strict subset of the role's own permissions.
    granted_permissions: tuple[str, ...] | None = None
    #: The file this scenario was loaded from, for error messages and result
    #: reporting. ``None`` for a scenario built directly in code (tests).
    source: pathlib.Path | None = None


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


def load_scenario(path: pathlib.Path) -> Scenario:
    """Parse one YAML scenario file.

    Raises `ScenarioError`, naming *path*, for any structural problem: not a
    mapping, missing/invalid `role`, empty or non-string `turns`, or an
    assertion field of the wrong type.
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
    )

    raw_granted = raw.get("granted_permissions")
    granted_permissions = (
        _require_str_tuple(raw_granted, field="granted_permissions", source=path)
        if raw_granted is not None
        else None
    )

    client = raw.get("client")
    if client is not None and not isinstance(client, str):
        raise ScenarioError(f"{path}: 'client' must be a string, got {client!r}")

    return Scenario(
        name=str(raw.get("name") or path.stem),
        role=role,
        turns=turns,
        assertions=assertions,
        description=str(raw.get("description") or ""),
        client=client,
        granted_permissions=granted_permissions,
        source=path,
    )


def load_scenarios(directory: pathlib.Path) -> tuple[Scenario, ...]:
    """Load every ``*.yaml``/``*.yml`` scenario file directly under *directory*,
    sorted by file name for a deterministic run order."""
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    return tuple(load_scenario(path) for path in sorted(paths))
