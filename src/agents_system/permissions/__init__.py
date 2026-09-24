"""Public surface of the `Permission` class hierarchy (spec: "Open hierarchy
for downstream extension"): the `Permission` root, the built-in action
families, the package-global `PermissionRegistry`, `resolve()`/`register()`
convenience wrappers, and the `AgentPermissionError` exception tree.
"""

from __future__ import annotations

from .base import Permission
from .builtins import Exec, Read, Run, Send, Spawn, Write
from .errors import (
    AgentPermissionError,
    InvalidPermissionTierError,
    PermissionRegistrationCollisionError,
    UnknownPermissionNameError,
)
from .permission_registry import PermissionRegistry, permission_registry

__all__ = [
    "AgentPermissionError",
    "Exec",
    "InvalidPermissionTierError",
    "Permission",
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
    """Resolve a wire name via the package-global registry."""
    return permission_registry.resolve(name)


def register(cls: type[Permission], name: str) -> None:
    """Register a class under a wire name via the package-global registry."""
    permission_registry.register(cls, name)
