"""Conversation summarization port (issue #39).

This module deliberately contains no implementation. Summarizing a
conversation requires the conversation: where turns are stored, how they are
scoped to a session, and what a deployment is willing to feed to a model. The
platform stores none of that on its own behalf.

The stub this replaces returned one fixed sentence — "Customer asked about
product availability and confirmed a purchase of two units" — for every
session id it was ever given. A summarizer that answers without reading is not
a degraded summarizer; it is a fabricated account of a conversation that may
have said the opposite.

So the platform declares the seam and refuses to fill it, the same way
``services.orders.OrderWriter`` does.
"""

from __future__ import annotations

from typing import Any, Protocol


class ConversationSummarizer(Protocol):
    """Summarizes one stored conversation.

    Implementations own the transcript store and the summarization strategy.
    ``session`` is the turn-scoped SQLAlchemy ``AsyncSession`` the connector
    contract injects; an implementation backed by something else may ignore it.
    """

    async def summarize(
        self,
        session: Any,
        *,
        session_id: str,
        max_messages: int | None,
    ) -> dict[str, Any]:
        """Summarize the conversation identified by *session_id*.

        An implementation that cannot read the conversation must RAISE rather
        than return a summary of nothing. A summary is consumed as an account
        of what was said, so an invented one is not a partial answer — it is a
        false one.

        Args:
            session: Turn-scoped async session, or whatever the deployment
                binds in its place.
            session_id: Conversation identifier, verified non-empty by the
                connector.
            max_messages: Caller's cap on how many recent messages to consider,
                or ``None`` for the implementation's own default. It is passed
                through rather than applied by the connector, because only the
                implementation knows what a message is in its store.

        Returns:
            A dict describing the summary. ``summary`` is expected; everything
            else is the deployment's to define.
        """
        ...
