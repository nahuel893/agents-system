"""`Permission` root class and R1 subclass-tier-monotonicity enforcement
(spec: "Permission base class carries an explicit tier", "Subclass tier
monotonicity (R1)"). Enforced in `__init_subclass__`, at class-definition
time -- not deferred to first use, registration, or grant time
(design.md's Class API).
"""

from __future__ import annotations

from typing import Any, ClassVar

from agents_system.harness.registry import Tier

from .errors import InvalidPermissionTierError

#: Ordinal rank for each `Tier`, used to compare declared tiers explicitly
#: rather than relying on `Tier(str, Enum)`'s inherited `str` ordering,
#: which happens to sort correctly for T0-T3's single-digit suffixes today
#: but is not a guarantee the `Tier` enum itself makes.
_RANK: dict[Tier, int] = {Tier.T0: 0, Tier.T1: 1, Tier.T2: 2, Tier.T3: 3}


def tier_rank(tier: Tier) -> int:
    """Explicit ordinal rank for a `Tier` member -- the single source of
    truth every tier comparison in this package routes through (R1's own
    monotonicity check below, and R2a/R2b/R3 in `permission_registry.py`),
    instead of `<=`/`>=` on the `Tier` enum members directly.
    """
    return _RANK[tier]


class Permission:
    """Root of the permission class hierarchy -- carries no tier of its
    own; every concrete subclass must declare one.
    """

    tier: ClassVar[Tier]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls is Permission:
            return
        own_tier = cls.__dict__.get("tier")  # OWN attribute, not inherited via MRO
        parent_tier = getattr(cls.__mro__[1], "tier", None)
        if own_tier is None:
            if parent_tier is None:
                raise InvalidPermissionTierError(cls, reason="no tier declared")
            cls.tier = parent_tier  # plain inheritance, not an R1 violation
            return
        try:
            own_rank = tier_rank(own_tier)
        except (KeyError, TypeError) as error:
            raise InvalidPermissionTierError(
                cls,
                own_tier,
                parent_tier,
                reason=f"{own_tier!r} is not a valid Tier member",
            ) from error
        if parent_tier is not None and own_rank < tier_rank(parent_tier):
            raise InvalidPermissionTierError(cls, own_tier, parent_tier)
