"""Knowledge base port (issue #39).

This module deliberately contains no implementation. An organizational
knowledge base is the consuming business's content — its policies, its
coverage, its procedures — and the platform has none of its own.

The stub this replaces answered every query from three hardcoded hits about
wholesale discounts, delivery zones and claims. Worse than being wrong, it was
never empty: a query that matched nothing fell back to the first two hits, so
the model received a confident, on-topic-looking answer to a question the
deployment had no content for.

So the platform declares the seam and refuses to fill it, mirroring
``services.rag.CatalogSource``: the protocol lives with the platform, the
implementation with the deployment.
"""

from __future__ import annotations

from typing import Any, Protocol


class KnowledgeBase(Protocol):
    """Searches the deployment's knowledge content.

    Implementations own their retrieval strategy. ``session`` is the
    turn-scoped SQLAlchemy ``AsyncSession`` the connector contract injects; an
    implementation backed by something else may ignore it.
    """

    async def search(self, session: Any, *, query: str) -> dict[str, Any]:
        """Return the knowledge hits matching *query*.

        An empty result is a legitimate answer and must be returned as one —
        unlike the stub, which substituted unrelated hits rather than admit a
        miss. An implementation that could not run the search must RAISE, so
        the connector can report that the knowledge base is unavailable instead
        of presenting "no matches" for a search that never ran.

        Args:
            session: Turn-scoped async session, or whatever the deployment
                binds in its place.
            query: The caller's natural-language query, already stripped and
                verified non-empty by the connector.

        Returns:
            A dict describing the hits. ``results`` is expected; everything
            else is the deployment's to define.
        """
        ...
