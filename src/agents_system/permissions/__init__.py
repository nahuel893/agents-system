"""Public surface of the `Permission` class hierarchy (spec: "Open hierarchy
for downstream extension"): the `Permission` root, the built-in action
families, the package-global `PermissionRegistry`, `resolve()`/`register()`
convenience wrappers, the R2a/R2b `evaluate_tool_spec` and R3 `covers`
policy predicates, and the `AgentPermissionError` exception tree.
"""

from __future__ import annotations

from .base import Permission
from .builtins import (
    Exec,
    Query,
    Read,
    Run,
    Send,
    Spawn,
    Write,
    ensure_resource_registered,
    resource,
)
from .errors import (
    AgentPermissionError,
    InvalidPermissionTierError,
    PermissionFloorViolationError,
    PermissionRegistrationCollisionError,
    PermissionTierMismatchError,
    UnknownPermissionNameError,
    UntrustedInputGrantError,
)
from .permission_registry import (
    PermissionRegistry,
    covers,
    evaluate_tool_spec,
    permission_registry,
)

__all__ = [
    "AgentPermissionError",
    "Exec",
    "InvalidPermissionTierError",
    "Permission",
    "PermissionFloorViolationError",
    "PermissionRegistrationCollisionError",
    "PermissionRegistry",
    "PermissionTierMismatchError",
    "Query",
    "Read",
    "Run",
    "Send",
    "Spawn",
    "UnknownPermissionNameError",
    "UntrustedInputGrantError",
    "Write",
    "covers",
    "ensure_resource_registered",
    "evaluate_tool_spec",
    "register",
    "resolve",
    "resource",
]


def resolve(name: str) -> type[Permission]:
    """Resolve a wire name via the package-global registry."""
    return permission_registry.resolve(name)


def register(cls: type[Permission], name: str) -> None:
    """Register a class under a wire name via the package-global registry."""
    permission_registry.register(cls, name)
