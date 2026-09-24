"""Exception hierarchy for the permission class model.

Rooted at `PermissionError` (spec: "Permission error type hierarchy"). Each
subclass corresponds to exactly one failure mode of the class-based
`Permission`/`PermissionRegistry` model that replaces string-prefix
inference:

```
PermissionError
├── UnknownPermissionNameError            # registry lookup failure (forward or reverse)
├── PermissionRegistrationCollisionError  # name<->class collision
└── InvalidPermissionTierError            # R1 violation at class creation
```

`PermissionTierMismatchError`, `PermissionFloorViolationError` (R2a/R2b, at
`ToolSpec` construction) and `UntrustedInputGrantError` (R4, at load/grant
time) are added to this module in PR2, once the callers that raise them
exist; this PR1 module only defines the errors its own package (base,
builtins, registry) can itself raise.
"""

from __future__ import annotations

from typing import Any


class PermissionError(Exception):
    """Root of the permission-model exception hierarchy.

    Deliberately shadows the built-in `PermissionError` (an `OSError`
    subclass) only within this package's namespace -- this is the exact
    name the spec's error tree names as its root, and nothing in this
    package needs the built-in.
    """


class InvalidPermissionTierError(PermissionError):
    """R1 violation: a `Permission` subclass's own declared tier ranks
    below its parent's, or a concrete subclass declares no tier and has no
    parent tier to inherit. Raised from `Permission.__init_subclass__`, at
    class-definition time -- before the class object exists.
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


class UnknownPermissionNameError(PermissionError):
    """Raised by `PermissionRegistry.resolve`/`reverse` when the given wire
    name or class was never registered (spec: "Resolving an unregistered
    name fails loudly", "Reverse resolution of an unregistered class
    fails"). Exactly one of `name`/`cls` is set, depending on which
    direction of lookup failed.
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


class PermissionRegistrationCollisionError(PermissionError):
    """Raised by `PermissionRegistry.register` when a registration would
    violate the one-name-per-class / one-class-per-name invariant (spec:
    "Name/class uniqueness and collision"). Either `existing_cls` (a
    different class already holds `name`) or `existing_name` (`incoming_cls`
    already holds a different canonical name) is set -- never both.
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
