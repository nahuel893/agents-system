"""Tests for the `Permission` root class and R1 subclass tier monotonicity.

Strict TDD: written before `agents_system.permissions.base` exists. Covers
spec Requirements "Permission base class carries an explicit tier" and
"Subclass tier monotonicity (R1)".
"""

from __future__ import annotations

import pytest

from agents_system.harness.registry import Tier
from agents_system.permissions.base import Permission
from agents_system.permissions.errors import InvalidPermissionTierError

pytestmark = [pytest.mark.usefixtures("reset_permission_registry")]


def test_concrete_subclass_declares_a_tier() -> None:
    """A subclass that declares its own tier is valid and eligible for
    registration."""

    class Foo(Permission):
        tier = Tier.T1

    assert Foo.tier is Tier.T1


def test_concrete_subclass_omitting_tier_with_no_parent_tier_raises() -> None:
    """A direct `Permission` subclass that declares no tier, and whose
    parent (the abstract root) carries none either, must fail creation."""

    with pytest.raises(InvalidPermissionTierError):

        class Bar(Permission):
            pass


def test_equal_tier_subclass_is_valid() -> None:
    class Base(Permission):
        tier = Tier.T0

    class Same(Base):
        tier = Tier.T0

    assert Same.tier is Tier.T0


def test_escalating_subclass_is_valid() -> None:
    class Base(Permission):
        tier = Tier.T2

    class Escalated(Base):
        tier = Tier.T3

    assert Escalated.tier is Tier.T3


def test_deescalating_subclass_raises_before_class_object_exists() -> None:
    """The class statement itself must raise -- not a later call -- so a
    de-escalating subclass never becomes a usable object."""

    class Base(Permission):
        tier = Tier.T2

    with pytest.raises(InvalidPermissionTierError):

        class DeEscalated(Base):
            tier = Tier.T1

    assert "DeEscalated" not in dir()  # class statement raised -- name never bound


def test_subclass_omitting_tier_override_inherits_parents_tier() -> None:
    """Omitting `tier` on a subclass of a tiered parent is plain
    inheritance, not an R1 violation -- distinguished via
    `cls.__dict__.get("tier")`, not `getattr`."""

    class Base(Permission):
        tier = Tier.T0

    class Inherited(Base):
        pass

    assert Inherited.tier is Tier.T0
    # Inheritance is recorded explicitly in the subclass's own __dict__,
    # not left to implicit MRO lookup (design.md's Class API note).
    assert "tier" in Inherited.__dict__
