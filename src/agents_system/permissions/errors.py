"""Exception hierarchy for the permission class model, rooted at
`AgentPermissionError` (spec: "Permission error type hierarchy"):

```
AgentPermissionError
├── UnknownPermissionNameError            # registry lookup failure (forward or reverse)
├── PermissionRegistrationCollisionError  # name<->class collision
├── InvalidPermissionTierError            # R1 violation at class creation
├── PermissionTierMismatchError           # R2a (ceiling) violation at ToolSpec construction
├── PermissionFloorViolationError         # R2b (floor) violation at ToolSpec construction
└── UntrustedInputGrantError              # R4 violation at load or grant/equip time
```

`PermissionFloorViolationError` is a distinct sibling of
`PermissionTierMismatchError`, not a reuse -- R2a and R2b are independent
predicates, so a caller catching one MUST NOT need to inspect the message
to know which check failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class AgentPermissionError(Exception):
    """Root of the permission-model exception hierarchy."""


class InvalidPermissionTierError(AgentPermissionError):
    """R1 violation: a subclass's declared tier is invalid, or ranks below
    its parent's. Raised from `Permission.__init_subclass__`.
    """

    def __init__(
        self,
        cls: type[Any],
        own_tier: Any = None,
        parent_tier: Any = None,
        *,
        reason: str | None = None,
    ) -> None:
        self.cls = cls
        self.own_tier = own_tier
        self.parent_tier = parent_tier
        self.reason = reason
        if reason is not None:
            message = f"{cls.__name__}: {reason}"
        else:
            message = (
                f"{cls.__name__}: declared tier {own_tier!r} is lower than "
                f"its parent's declared tier {parent_tier!r} -- a Permission "
                f"subclass's tier must be >= its parent's tier (R1)"
            )
        super().__init__(message)


class UnknownPermissionNameError(AgentPermissionError):
    """A wire name or class was never registered (forward or reverse
    lookup). Exactly one of `name`/`cls` is set.
    """

    def __init__(
        self, *, name: str | None = None, cls: type[Any] | None = None
    ) -> None:
        self.name = name
        self.cls = cls
        if name is not None:
            message = f"no permission registered under name {name!r}"
        elif cls is not None:
            message = f"class {cls.__name__} was never registered under any name"
        else:
            message = "unknown permission"
        super().__init__(message)


class PermissionRegistrationCollisionError(AgentPermissionError):
    """A registration would violate the one-name-per-class /
    one-class-per-name invariant. Either `existing_cls` or `existing_name`
    is set -- never both.
    """

    def __init__(
        self,
        name: str,
        *,
        existing_cls: type[Any] | None = None,
        incoming_cls: type[Any] | None = None,
        existing_name: str | None = None,
    ) -> None:
        self.name = name
        self.existing_cls = existing_cls
        self.incoming_cls = incoming_cls
        self.existing_name = existing_name
        if existing_name is not None and incoming_cls is not None:
            message = (
                f"class {incoming_cls.__name__} is already registered under "
                f"{existing_name!r}; cannot also register it under {name!r} "
                f"(a class holds exactly one canonical name)"
            )
        elif existing_cls is not None and incoming_cls is not None:
            message = (
                f"name {name!r} is already registered to {existing_cls.__name__}; "
                f"cannot register {incoming_cls.__name__} under the same name"
            )
        else:
            message = f"registration collision for name {name!r}"
        super().__init__(message)


class PermissionTierMismatchError(AgentPermissionError):
    """R2a (ceiling) violation: a `ToolSpec` requires a permission whose
    declared tier exceeds the tool's own tier (`t >= p.tier` failed).
    Raised from `evaluate_tool_spec` at `ToolSpec` construction.
    """

    def __init__(
        self,
        tool_name: str,
        tool_tier: Any,
        permission_name: str,
        permission_cls: type[Any],
    ) -> None:
        self.tool_name = tool_name
        self.tool_tier = tool_tier
        self.permission_name = permission_name
        self.permission_cls = permission_cls
        super().__init__(
            f"ToolSpec {tool_name!r} (tier={tool_tier.value}): required "
            f"permission {permission_name!r} ({permission_cls.__name__}, "
            f"tier={permission_cls.tier.value}) exceeds the tool's own tier "
            "-- R2a requires tool tier >= permission tier."
        )


class PermissionFloorViolationError(AgentPermissionError):
    """R2b (floor) violation: a T2/T3 `ToolSpec`'s required permissions
    never reach the tool's own tier -- a distinct sibling of
    `PermissionTierMismatchError`, not a reuse (R2a and R2b are
    independent predicates). Raised from `evaluate_tool_spec`.
    """

    def __init__(
        self,
        tool_name: str,
        tool_tier: Any,
        evaluated: Sequence[tuple[str, type[Any]]],
    ) -> None:
        self.tool_name = tool_name
        self.tool_tier = tool_tier
        self.evaluated = tuple(evaluated)
        details = (
            ", ".join(
                f"{name!r} ({cls.__name__}, tier={cls.tier.value})"
                for name, cls in evaluated
            )
            or "(none declared)"
        )
        super().__init__(
            f"ToolSpec {tool_name!r} (tier={tool_tier.value}): none of its "
            "required permissions reaches the tool's own tier -- R2b "
            "requires at least one required permission's tier >= the "
            f"tool's tier. Evaluated: {details}."
        )


class UntrustedInputGrantError(AgentPermissionError):
    """R4 violation: an `untrusted_input` role or grant holds a T3-tier
    permission, regardless of its registered wire name. Raised at
    definition load (`harness.loader`) and at deploy-time grant/equip.
    """

    def __init__(
        self, permission_name: str, permission_cls: type[Any], role_name: str
    ) -> None:
        self.permission_name = permission_name
        self.permission_cls = permission_cls
        self.role_name = role_name
        super().__init__(
            f"role {role_name!r}: permission {permission_name!r} "
            f"({permission_cls.__name__}, tier={permission_cls.tier.value}) "
            "is tier T3 -- untrusted_input roles may never hold a T3 "
            "permission (R4)."
        )
