"""Public surface of the `Permission` class hierarchy.

Replaces string-prefix inference (`exec:`, `write:`, `send:`, `run:`) as the
source of safety truth (spec.md's Purpose section): the `Permission` root
and R1 tier monotonicity (`base.py`), the five built-in action families
plus `Spawn` (`builtins.py`), the package-global thread-safe
`PermissionRegistry` (`permission_registry.py`), and the `PermissionError`
exception tree (`errors.py`) are all re-exported here as the one import
root a downstream package needs (spec: "Open hierarchy for downstream
extension").

`resolve()`/`register()` below are convenience wrappers delegating to the
package-global `permission_registry` instance -- equivalent to calling
`permission_registry.resolve(...)`/`.register(...)` directly.
"""

from __future__ import annotations

from .base import Permission
from .builtins import Exec, Read, Run, Send, Spawn, Write
from .errors import (
    InvalidPermissionTierError,
    PermissionError,
    PermissionRegistrationCollisionError,
    UnknownPermissionNameError,
)
from .permission_registry import PermissionRegistry, permission_registry

__all__ = [
    "Exec",
    "InvalidPermissionTierError",
    "Permission",
    "PermissionError",
    "PermissionRegistrationCollisionError",
    "PermissionRegistry",
    "Read",
    "Run",
    "Send",
    "Spawn",
    "UnknownPermissionNameError",
    "Write",
    "register",
    "resolve",
]


def resolve(name: str) -> type[Permission]:
    """Resolve a registered wire name to its `Permission` class via the
    package-global registry."""
    return permission_registry.resolve(name)


def register(cls: type[Permission], name: str) -> None:
    """Register a `Permission` class under a wire name via the
    package-global registry."""
    permission_registry.register(cls, name)
