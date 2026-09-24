"""Thread-safe registry resolving wire-name strings to `Permission`
classes and back (spec: "Explicit registration, no inference from string
structure", "Name/class uniqueness and collision", "Lookup by name and by
class", "Registry thread-safety"). No prefix, substring, or pattern
parsing of a wire name is ever performed -- resolution is a plain dict
lookup against explicitly registered entries only.
"""

from __future__ import annotations

import threading

from agents_system.harness.registry import Tier

from .base import Permission
from .errors import (
    InvalidPermissionTierError,
    PermissionFloorViolationError,
    PermissionRegistrationCollisionError,
    PermissionTierMismatchError,
    UnknownPermissionNameError,
)


class PermissionRegistry:
    """Explicit name <-> class resolution for `Permission` subclasses.
    One `threading.Lock` guards both `_by_name` and `_by_class`.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, type[Permission]] = {}
        self._by_class: dict[type[Permission], str] = {}
        self._lock = threading.Lock()

    def register(self, cls: type[Permission], name: str) -> None:
        """Register `cls` under wire name `name`. Idempotent on an exact
        `cls`/`name` match; raises `PermissionRegistrationCollisionError`
        when `name` or `cls` is already bound to something else, and
        `InvalidPermissionTierError` for the abstract `Permission` root
        itself (spec: never usable as a required or granted permission).
        """
        if cls is Permission:
            raise InvalidPermissionTierError(
                cls,
                reason="the abstract Permission root carries no tier and "
                "cannot be registered as a required or granted permission",
            )
        with self._lock:
            existing_cls = self._by_name.get(name)
            existing_name = self._by_class.get(cls)
            if existing_cls is cls and existing_name == name:
                return  # exact re-registration -- idempotent, per spec
            if existing_cls is not None:
                raise PermissionRegistrationCollisionError(
                    name, existing_cls=existing_cls, incoming_cls=cls
                )
            if existing_name is not None:
                raise PermissionRegistrationCollisionError(
                    name, incoming_cls=cls, existing_name=existing_name
                )
            self._by_name[name] = cls
            self._by_class[cls] = name

    def resolve(self, name: str) -> type[Permission]:
        """Forward resolution: wire name -> registered class."""
        with self._lock:
            try:
                return self._by_name[name]
            except KeyError as error:
                raise UnknownPermissionNameError(name=name) from error

    def reverse(self, cls: type[Permission]) -> str:
        """Reverse resolution: registered class -> its canonical wire name."""
        with self._lock:
            try:
                return self._by_class[cls]
            except KeyError as error:
                raise UnknownPermissionNameError(cls=cls) from error

    def _snapshot(
        self,
    ) -> tuple[dict[str, type[Permission]], dict[type[Permission], str]]:
        """Test-only: capture state for a later `_restore` call."""
        with self._lock:
            return dict(self._by_name), dict(self._by_class)

    def _restore(
        self,
        snapshot: tuple[dict[str, type[Permission]], dict[type[Permission], str]],
    ) -> None:
        """Test-only: restore state captured by a prior `_snapshot` call."""
        by_name, by_class = snapshot
        with self._lock:
            self._by_name = dict(by_name)
            self._by_class = dict(by_class)


#: Package-global registry instance every built-in and downstream
#: permission is registered into.
permission_registry = PermissionRegistry()


def evaluate_tool_spec(
    name: str, tier: Tier, required_permissions: tuple[str, ...]
) -> None:
    """R2a (ceiling) + R2b (floor): validate a `ToolSpec`'s required
    permissions against its own declared tier. Called from
    `ToolSpec.__post_init__` via a deferred import (design.md's Cycle
    Avoidance); never mutates the registry, only resolves and compares.

    R2a: every required permission's tier MUST NOT exceed the tool's own
    tier (`t >= p.tier`) -- raises `PermissionTierMismatchError`.
    R2b: a T2/T3 tool MUST require at least one permission whose tier
    reaches its own (`max(p.tier for p in required) >= t`) -- raises
    `PermissionFloorViolationError`. Independent predicates; both run and
    both must pass.
    """
    resolved = [
        (perm_name, permission_registry.resolve(perm_name))
        for perm_name in required_permissions
    ]
    for perm_name, cls in resolved:
        if not (tier >= cls.tier):
            raise PermissionTierMismatchError(name, tier, perm_name, cls)

    if tier in (Tier.T2, Tier.T3) and not any(cls.tier >= tier for _, cls in resolved):
        raise PermissionFloorViolationError(name, tier, resolved)


def covers(granted: type[Permission], required: type[Permission]) -> bool:
    """R3: does a grant of `granted` cover a required permission `required`?

    True when `required` is `granted` itself or a registered subclass of it,
    AND `required`'s declared tier does not exceed `granted`'s (`D.tier <=
    P.tier`) -- a descendant that escalated its own tier needs its own
    explicit grant.
    """
    return issubclass(required, granted) and required.tier <= granted.tier
