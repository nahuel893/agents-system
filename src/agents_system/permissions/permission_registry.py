"""Thread-safe registry resolving wire-name strings to `Permission` classes
and back.

Spec Requirements: "Explicit registration, no inference from string
structure", "Name/class uniqueness and collision", "Lookup by name and by
class", "Registry thread-safety". No prefix, substring, or pattern parsing
of a wire name is ever performed here -- resolution is a plain dict lookup
against explicitly registered entries only.

`permission_registry` (the module-level instance below) is the one
package-global registry every built-in and downstream permission is
registered into (design.md's PermissionRegistry section).
"""

from __future__ import annotations

import threading

from .base import Permission
from .errors import PermissionRegistrationCollisionError, UnknownPermissionNameError


class PermissionRegistry:
    """Explicit name <-> class resolution for `Permission` subclasses.

    Two dicts (`_by_name`, `_by_class`) guarded by one `threading.Lock`
    taken on both read and write paths -- call volume is registration-time
    bounded (once at import, occasionally at downstream extension), not a
    per-request hot path, so lock-free reads are not worth the added
    complexity (design.md's PermissionRegistry section).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, type[Permission]] = {}
        self._by_class: dict[type[Permission], str] = {}
        self._lock = threading.Lock()

    def register(self, cls: type[Permission], name: str) -> None:
        """Register `cls` under wire name `name`.

        Idempotent when `cls`/`name` exactly match an existing entry (spec:
        "Idempotent re-registration"). Raises
        `PermissionRegistrationCollisionError` when `name` already resolves
        to a *different* class, or when `cls` already holds a *different*
        canonical name -- a class holds exactly one canonical name, and a
        name resolves to exactly one class.
        """
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
        """Forward resolution: wire name -> registered class.

        Raises `UnknownPermissionNameError` naming the exact requested
        string when `name` was never registered.
        """
        with self._lock:
            try:
                return self._by_name[name]
            except KeyError as error:
                raise UnknownPermissionNameError(name=name) from error

    def reverse(self, cls: type[Permission]) -> str:
        """Reverse resolution: registered class -> its canonical wire name.

        Raises `UnknownPermissionNameError` when `cls` was never
        registered.
        """
        with self._lock:
            try:
                return self._by_class[cls]
            except KeyError as error:
                raise UnknownPermissionNameError(cls=cls) from error

    def _snapshot(
        self,
    ) -> tuple[dict[str, type[Permission]], dict[type[Permission], str]]:
        """Test-only: capture current state for a later `_restore` call
        (leading underscore -- not part of the public API; design.md's Test
        Isolation note).
        """
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
