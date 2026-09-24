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


def test_subclass_with_non_tier_value_raises_not_keyerror() -> None:
    """A garbage `tier` value (not a `Tier` member) must raise
    `InvalidPermissionTierError`, not a raw `KeyError` from the internal
    `_RANK` ordinal lookup -- regardless of whether there is a parent tier
    to compare against."""

    with pytest.raises(InvalidPermissionTierError):

        class DirectlyBadTier(Permission):
            tier = "not_a_real_tier"

    class Base(Permission):
        tier = Tier.T1

    with pytest.raises(InvalidPermissionTierError):

        class BadTierWithParent(Base):
            tier = "still_not_a_tier"


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


# ---------------------------------------------------------------------------
# `tier_rank` pins the explicit ordinal every tier comparison in this
# package (R1's own monotonicity check here, plus R2a/R2b/R3 in
# `permission_registry.py`) routes through, instead of relying on
# `Tier(str, Enum)`'s inherited `str` ordering. `Tier`'s values ("T0".."T3")
# happen to sort correctly today because each is a single digit, but that
# is an implementation accident of the string representation, not a
# guarantee the `Tier` enum itself makes -- a future tier renamed to
# something whose string ordering diverges from its intended rank (or a
# member reordering) would silently invert every ceiling/floor/coverage
# check if comparisons used `<=`/`>=` on the enum members directly. Pinning
# the explicit mapping here catches that class of regression immediately.
# ---------------------------------------------------------------------------
def test_tier_rank_pins_the_explicit_ordinal_mapping() -> None:
    from agents_system.permissions.base import tier_rank

    assert tier_rank(Tier.T0) == 0
    assert tier_rank(Tier.T1) == 1
    assert tier_rank(Tier.T2) == 2
    assert tier_rank(Tier.T3) == 3


def test_tier_rank_orders_strictly_increasing() -> None:
    from agents_system.permissions.base import tier_rank

    ranks = [tier_rank(t) for t in (Tier.T0, Tier.T1, Tier.T2, Tier.T3)]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 4  # strictly increasing, no ties
