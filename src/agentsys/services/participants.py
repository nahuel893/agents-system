"""Who is talking to the agent, and what was said.

An inbound channel delivery carries an address (a phone number, a Telegram
chat id) that has to become an identity before a turn may run, and a
completed turn has to be recorded. Both answers depend on the consumer's own
schema: `agentsys` has no clients table and must not grow one.

So both arrive as protocols. `integration/webhook.py` used to import
`services.clients` and `services.conversation_log` directly, which made the
platform's inbound route depend on one deployment's `clients` and
`conversation_logs` tables.

The seam is the same one `services.rag.CatalogSource` and
`services.embeddings.EmbeddingProvider` already use.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Participant(Protocol):
    """The identity a channel address resolves to.

    `active` is what gates the turn: an inactive participant is a known
    address the deployment has chosen not to serve, which is a different
    answer from an unknown one and must not be collapsed into it.
    """

    @property
    def id(self) -> Any:
        """Stable identifier, whatever the consumer's key type is."""
        ...

    @property
    def active(self) -> bool:
        """False when this participant exists but must not be served."""
        ...


class ParticipantDirectory(Protocol):
    """Resolves an inbound channel address to a participant."""

    def normalize_address(self, raw: str) -> str:
        """Return the canonical form of a channel-supplied address.

        Raise `ValueError` when *raw* cannot be parsed. The caller treats
        that as a dropped delivery, never as a server error — an unparseable
        address is attacker- or vendor-controlled input, and answering 5xx
        would make the sender retry a message that can never succeed.

        Normalization is here rather than on the channel because today the
        only implementation is phone-number based. When the channel port
        lands (#71) this may move to the adapter, where it arguably belongs.
        """
        ...

    async def resolve(self, session: Any, address: str) -> Participant | None:
        """Return the participant for *address*, or None if there is none.

        Receives the caller's session and MUST NOT commit or roll it back —
        the same transaction contract connectors follow (D-009).
        """
        ...


class ConversationRecorder(Protocol):
    """Records a completed turn wherever the consumer keeps its history."""

    async def record_turn(
        self,
        session: Any,
        *,
        thread_id: str,
        participant_id: Any,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Persist one user/assistant exchange.

        Best-effort by contract: the caller runs this in its own session and
        swallows failures, because losing an audit row must never cost the
        customer their reply.
        """
        ...
