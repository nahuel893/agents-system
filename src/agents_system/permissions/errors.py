"""Exception hierarchy for the permission class model, rooted at
`AgentPermissionError` (spec: "Permission error type hierarchy"). PR2 adds
`PermissionTierMismatchError`, `PermissionFloorViolationError`, and
`UntrustedInputGrantError` once the callers that raise them exist; this
module only defines the errors PR1's own package can itself raise:

```
AgentPermissionError
├── UnknownPermissionNameError            # registry lookup failure (forward or reverse)
├── PermissionRegistrationCollisionError  # name<->class collision
└── InvalidPermissionTierError            # R1 violation at class creation
```
"""

from __future__ import annotations

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
