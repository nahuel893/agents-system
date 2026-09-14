"""Escalation port (issue #39).

This module deliberately contains no implementation. Reaching a human means
knowing who the humans are, which channel they watch, what hours they keep and
what counts as urgent — those are the consuming business's facts, not the
platform's.

The stub this replaces made that concrete: it returned
``{"status": "notified", "escalation_id": "esc-0001"}`` from a process counter,
with no channel behind it. Every role in the taxonomy inherits
``escalation_notifier``, so the single path a stuck customer has was the one
that reported success while nobody was told.

So the platform declares the seam and refuses to fill it, the same way
``services.orders.OrderWriter`` and ``services.rag.CatalogSource`` do: the
protocol lives with the platform, the implementation with the deployment.
"""

from __future__ import annotations

from typing import Any, Protocol


class EscalationChannel(Protocol):
    """Delivers an escalation to a human operator.

    Implementations own the delivery and its transaction. ``session`` is the
    turn-scoped SQLAlchemy ``AsyncSession`` the connector contract injects; an
    implementation backed by something else (Slack, PagerDuty, a queue) may
    ignore it.
    """

    async def notify(
        self,
        session: Any,
        *,
        reason: str,
        details: str,
    ) -> dict[str, Any]:
        """Put a human on this conversation and return what identifies it.

        Returning normally asserts that a human was actually reached. An
        implementation that could not deliver must RAISE rather than return a
        success shape — the connector turns a raised failure into an honest
        "nobody was notified", and it cannot do that for a lie it was handed.

        Args:
            session: Turn-scoped async session, or whatever the deployment
                binds in its place.
            reason: Short machine-ish reason, e.g. ``customer_angry``.
            details: Supporting context the human needs in order to act.

        Returns:
            A dict describing the delivered escalation. ``escalation_id`` is
            REQUIRED: the connector refuses to report an escalation it cannot
            identify, because "a human was reached" is precisely the claim that
            must never be guessed.
        """
        ...
