"""ConversationLog audit writer (D-014 S4, design AD-6).

The WEBHOOK HANDLER owns this write in its OWN session/transaction — the
agent turn (run_turn / _execute_tools) only ever owns the turn-scoped TOOL
session (D-012 AD#5). This keeps the audit trail's durability independent
from the agent loop's own DB usage: the checkpointer (Redis) is the live
agent state, ConversationLog (Postgres) is the durable human-readable audit
trail.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from agentsys.models.tables import ConversationLog


async def log_conversation_turn(
    session: AsyncSession,
    *,
    thread_id: str,
    client_id: int | None,
    user_text: str,
    assistant_text: str,
    model_used: str | None = None,
    tokens_used: int | None = None,
) -> None:
    """Add one 'user' row and one 'assistant' row for a completed turn.

    Does NOT commit — the caller (the webhook handler) owns the transaction
    boundary and commits once both rows are added, so a write failure here
    can be caught and swallowed by the caller without a partial commit.
    ``tokens_used``/``model_used`` are best-effort: absent (``None``)
    persists as SQL NULL rather than raising.
    """
    session.add(
        ConversationLog(
            thread_id=thread_id,
            client_id=client_id,
            role="user",
            content=user_text,
        )
    )
    session.add(
        ConversationLog(
            thread_id=thread_id,
            client_id=client_id,
            role="assistant",
            content=assistant_text,
            model_used=model_used,
            tokens_used=tokens_used,
        )
    )
