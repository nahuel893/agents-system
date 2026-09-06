"""Order persistence port (issue #39).

This module deliberately contains no implementation. Writing an order means
knowing what an order IS — line pricing, discounts, stock reservation, tax,
which statuses count as sold — and those are the consuming business's rules,
not the platform's. A default implementation here would be a guess, and the
stub it replaces proved how a guess behaves: it reported `status: created`
for an order that existed nowhere.

So the platform declares the seam and refuses to fill it. An application that
has orders supplies an `OrderWriter` and registers the tool over it; an
application that does not gets a tool that says so.

This mirrors `services.rag.CatalogSource` and `services.participants
.ParticipantDirectory`: the protocol lives with the platform, the
implementation with the deployment.
"""

from __future__ import annotations

from typing import Any, Protocol


class OrderWriter(Protocol):
    """Persists an order for a client.

    Implementations own the transaction. `session` is the turn-scoped
    SQLAlchemy `AsyncSession` the connector contract injects; an implementation
    backed by something other than that session may ignore it.
    """

    async def create_order(
        self,
        session: Any,
        *,
        client_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create one order and return what the agent may tell the customer.

        Returning normally asserts the order was persisted. An implementation
        that could not persist must raise rather than return a success shape —
        the connector turns a raised failure into an honest "not created", and
        it cannot do that for a lie it was handed.

        Args:
            session: Turn-scoped async session, or whatever the deployment
                binds in its place.
            client_id: Identifier from the client lookup tool.
            items: Line items, each `{"product_id": str, "qty": int}`.

        Returns:
            A dict describing the persisted order. `order_id` is expected;
            everything else is the deployment's to define.
        """
        ...
