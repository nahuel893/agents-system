"""Tests for `agents_system.permissions`'s public package surface.

Strict TDD: written before `permissions/__init__.py` re-exports its public
API (it currently only carries a placeholder docstring). Covers the import
surface a downstream package relies on (spec: "Open hierarchy for
downstream extension").
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.usefixtures("reset_permission_registry")]


def test_permission_classes_are_importable_from_the_package_root() -> None:
    from agents_system.permissions import (
        Exec,
        Permission,
        PermissionRegistry,
        Read,
        Run,
        Send,
        Spawn,
        Write,
    )

    assert issubclass(Read, Permission)
    assert issubclass(Write, Permission)
    assert issubclass(Send, Permission)
    assert issubclass(Exec, Permission)
    assert issubclass(Run, Permission)
    assert issubclass(Spawn, Permission)
    assert PermissionRegistry is not None


def test_module_level_resolve_and_register_delegate_to_the_package_global_registry() -> (
    None
):
    from agents_system.permissions import Permission, register, resolve
    from agents_system.permissions.permission_registry import permission_registry

    class _ExportsProbe(Permission):
        tier = permission_registry.resolve("read:catalog").tier

    register(_ExportsProbe, "test:exports_probe")

    assert resolve("test:exports_probe") is _ExportsProbe
    assert permission_registry.resolve("test:exports_probe") is _ExportsProbe


def test_error_hierarchy_is_importable_from_the_package_root() -> None:
    from agents_system.permissions import (
        AgentPermissionError,
        InvalidPermissionTierError,
        PermissionRegistrationCollisionError,
        UnknownPermissionNameError,
    )

    assert issubclass(InvalidPermissionTierError, AgentPermissionError)
    assert issubclass(UnknownPermissionNameError, AgentPermissionError)
    assert issubclass(PermissionRegistrationCollisionError, AgentPermissionError)
