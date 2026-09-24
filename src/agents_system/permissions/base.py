"""`Permission` root class and R1 subclass-tier-monotonicity enforcement.

Spec Requirements: "Permission base class carries an explicit tier",
"Subclass tier monotonicity (R1)". The abstract `Permission` root carries no
tier (it exists only as the hierarchy root -- never usable directly as a
required or granted permission); every concrete subclass must declare a
`tier: Tier` class attribute, enforced here at class-definition time via
`__init_subclass__`, not deferred to first use, registration, or grant time
(design.md's Class API).
"""

from __future__ import annotations

from typing import Any, ClassVar

from agents_system.harness.registry import Tier

from .errors import InvalidPermissionTierError

#: Ordinal rank for each `Tier`, used to compare declared tiers for R1
#: monotonicity (a subclass's tier must rank >= its parent's).
_RANK: dict[Tier, int] = {Tier.T0: 0, Tier.T1: 1, Tier.T2: 2, Tier.T3: 3}


class Permission:
    """Root of the permission class hierarchy.

    `tier` is annotated but never assigned here -- the abstract root has no
    tier of its own, only concrete subclasses do. Subclassing is open to any
    downstream package (spec: "Open hierarchy for downstream extension");
    `__init_subclass__` never auto-registers a new class with the registry
    (spec: "there is no auto-discovery") -- registration is always an
    explicit, separate call.
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
        if parent_tier is not None and _RANK[own_tier] < _RANK[parent_tier]:
            raise InvalidPermissionTierError(cls, own_tier, parent_tier)
