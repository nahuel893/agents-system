"""Tests for `PermissionRegistry`: resolve/reverse/register, collision,
idempotent re-registration, and thread-safety.

Strict TDD: written before `agents_system.permissions.permission_registry`
exists. Covers spec Requirements "Explicit registration, no inference from
string structure", "Name/class uniqueness and collision", "Lookup by name
and by class", "Registry thread-safety".

Uses locally-defined dummy `Permission` subclasses throughout (not the
shipped built-ins from `permissions.builtins`, which depend on this very
module) so this file has no dependency on PR1-T2's `builtins.py`.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_system.harness.registry import Tier
from agents_system.permissions.base import Permission
from agents_system.permissions.errors import (
    InvalidPermissionTierError,
    PermissionRegistrationCollisionError,
    UnknownPermissionNameError,
)
from agents_system.permissions.permission_registry import PermissionRegistry

pytestmark = [pytest.mark.usefixtures("reset_permission_registry")]


class _Alpha(Permission):
    tier = Tier.T0


class _Beta(Permission):
    tier = Tier.T1


def test_resolve_returns_the_registered_class() -> None:
    registry = PermissionRegistry()
    registry.register(_Alpha, "test:alpha")

    assert registry.resolve("test:alpha") is _Alpha


def test_registering_the_abstract_permission_root_raises() -> None:
    """The abstract root carries no tier and MUST NOT be usable as a
    required/granted permission (spec: "the abstract Permission root MUST
    NOT be directly usable as a required or granted permission")."""
    registry = PermissionRegistry()

    with pytest.raises(InvalidPermissionTierError):
        registry.register(Permission, "test:bare_root")

    with pytest.raises(UnknownPermissionNameError):
        registry.resolve("test:bare_root")


def test_resolve_of_unregistered_name_raises_naming_the_exact_string() -> None:
    registry = PermissionRegistry()

    with pytest.raises(UnknownPermissionNameError) as exc_info:
        registry.resolve("test:never_registered")

    assert exc_info.value.name == "test:never_registered"
    assert "test:never_registered" in str(exc_info.value)


def test_reregistering_same_class_same_name_is_idempotent() -> None:
    registry = PermissionRegistry()
    registry.register(_Alpha, "test:alpha")

    registry.register(_Alpha, "test:alpha")  # must not raise

    assert registry.resolve("test:alpha") is _Alpha
    assert registry.reverse(_Alpha) == "test:alpha"


def test_registering_different_class_under_bound_name_collides() -> None:
    registry = PermissionRegistry()
    registry.register(_Alpha, "test:shared_name")

    with pytest.raises(PermissionRegistrationCollisionError) as exc_info:
        registry.register(_Beta, "test:shared_name")

    assert exc_info.value.existing_cls is _Alpha
    assert exc_info.value.incoming_cls is _Beta


def test_registering_same_class_under_second_name_collides() -> None:
    registry = PermissionRegistry()
    registry.register(_Alpha, "test:first_name")

    with pytest.raises(PermissionRegistrationCollisionError) as exc_info:
        registry.register(_Alpha, "test:second_name")

    assert exc_info.value.incoming_cls is _Alpha
    assert exc_info.value.existing_name == "test:first_name"


def test_reverse_resolution_returns_the_canonical_name() -> None:
    registry = PermissionRegistry()
    registry.register(_Alpha, "test:alpha")

    assert registry.reverse(_Alpha) == "test:alpha"


def test_reverse_resolution_of_unregistered_class_raises() -> None:
    registry = PermissionRegistry()

    class _NeverRegistered(Permission):
        tier = Tier.T0

    with pytest.raises(UnknownPermissionNameError) as exc_info:
        registry.reverse(_NeverRegistered)

    assert exc_info.value.cls is _NeverRegistered


def test_concurrent_resolves_against_a_pre_registered_registry() -> None:
    """Many threads resolving concurrently must each get a correct, fully
    registered class -- no corruption, no lost registration."""
    registry = PermissionRegistry()
    classes = []
    for i in range(20):
        cls = type(f"_Concurrent{i}", (Permission,), {"tier": Tier.T0})
        name = f"test:concurrent_{i}"
        registry.register(cls, name)
        classes.append((name, cls))

    def _resolve_many(name: str, expected: type[Permission]) -> bool:
        return all(registry.resolve(name) is expected for _ in range(200))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(_resolve_many, name, cls) for name, cls in classes * 5
        ]
        results = [future.result() for future in futures]

    assert all(results)


def test_get_or_register_holds_the_lock_across_lookup_and_create() -> None:
    """TOCTOU guard: many threads racing to get-or-register the SAME
    not-yet-registered name must trigger the factory exactly once and all
    observe the exact same resulting class.

    A naive `resolve()`-then-`register()` two-step (two separate lock
    acquisitions) leaves a window where two threads can both see "not
    registered yet", both build a class, and the second registration then
    either silently loses the first thread's class or spuriously raises
    `PermissionRegistrationCollisionError` for what the caller intended as
    an ordinary concurrent get-or-create. `get_or_register` must hold one
    lock across the whole lookup-then-create-then-insert sequence instead.
    """
    registry = PermissionRegistry()
    name = "test:concurrent_new"
    build_count = 0
    build_lock = threading.Lock()
    worker_count = 32
    start_barrier = threading.Barrier(worker_count)

    def _factory() -> type[Permission]:
        nonlocal build_count
        with build_lock:
            build_count += 1
        return type("_ConcurrentNew", (Permission,), {"tier": Tier.T0})

    def _get_or_register() -> type[Permission]:
        start_barrier.wait()  # maximize contention: all threads start together
        return registry.get_or_register(name, _factory)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_get_or_register) for _ in range(worker_count)]
        results = [future.result() for future in futures]

    assert build_count == 1
    assert len(set(results)) == 1
    assert registry.resolve(name) is results[0]


# ---------------------------------------------------------------------------
# Test isolation: `reset_permission_registry` (tests/conftest.py) must
# snapshot the package-global `permission_registry` before each test and
# restore it after, so a throwaway registration in one test cannot leak
# into the next (design.md's Test Isolation note). These two tests share
# the module-global registry deliberately -- unlike every other test above,
# which uses its own local `PermissionRegistry()` instance -- specifically
# to prove the fixture, not the registry class itself.
# ---------------------------------------------------------------------------


def test_throwaway_registration_is_visible_within_its_own_test() -> None:
    from agents_system.permissions.permission_registry import permission_registry

    class _IsolationThrowaway(Permission):
        tier = Tier.T0

    permission_registry.register(_IsolationThrowaway, "test:isolation_throwaway")

    assert (
        permission_registry.resolve("test:isolation_throwaway") is _IsolationThrowaway
    )


def test_throwaway_registration_does_not_leak_into_the_next_test() -> None:
    """Runs after the test above in the same session. Without the
    `reset_permission_registry` fixture restoring state between tests, the
    previous test's registration would still be resolvable here."""
    from agents_system.permissions.permission_registry import permission_registry

    with pytest.raises(UnknownPermissionNameError):
        permission_registry.resolve("test:isolation_throwaway")
