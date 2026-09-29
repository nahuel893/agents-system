"""The channel port (#22): what a messaging channel does, and nothing else.

A channel does three things:

1. **Authenticate** an inbound delivery (``verify_inbound``).
2. **Normalize** its payload -- at the edge into the messages the durable
   inbox accepts (``read_inbound``), later into the one turn the agent runs
   (``extract_turn``).
3. **Send** a reply back out (``send_text``).

Everything between those ends -- the bounded body read, durable acceptance
and dedup, the deferred worker, participant lookup, running the turn,
recording it -- is channel-agnostic and stays in the platform. The generic
``POST /webhook/{channel}`` route (``integration/webhook.py``) and
``DeferredWebhookWorker`` (``services/webhook_worker.py``) reach a channel
only through this port.

A deployment adds a channel with ``create_app(channels={"name":
Channel(adapter=..., runtime_id=...)})``: no platform module is edited. The
adapter owns its own configuration (a token, a secret, an HTTP client) --
nothing about it is added to the platform's flat ``Settings``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agents_system.extras import raise_missing_extra

try:
    from starlette.datastructures import Headers
except ImportError as exc:
    raise_missing_extra("server", "The channel port", exc)

if TYPE_CHECKING:
    from agents_system.config import Settings


@dataclass(frozen=True)
class InboundRef:
    """One message a verified delivery carries, as the durable inbox keys it.

    ``message_id`` is the channel's own id for the message: the inbox dedups
    on it (scoped to the channel, see ``services.outbox.channel_scoped_key``),
    so a redelivery of the same id is accepted once. ``conversation_key`` is
    what must be processed in arrival order -- usually the sender -- and
    must never be empty: a message with no usable sender needs a key of its
    own (e.g. derived from its id) so it cannot block anyone else.
    """

    message_id: str
    conversation_key: str


@dataclass(frozen=True)
class InboundDelivery:
    """A verified delivery, normalized for durable acceptance.

    ``payload`` is stored verbatim against every message in ``messages`` and
    handed back to ``ChannelAdapter.extract_turn`` when the deferred worker
    processes one of them, so it must be JSON-serializable. An empty
    ``messages`` (a status callback, a malformed body) acknowledges the
    delivery and persists nothing.
    """

    payload: dict[str, Any]
    messages: tuple[InboundRef, ...]


@dataclass(frozen=True)
class InboundTurn:
    """The one turn a stored message asks for.

    ``sender`` is the channel-native address; the platform normalizes and
    resolves it through the deployment's participant directory, together
    with this channel's name (``ChannelParticipantDirectory``), and replies
    to the normalized form via ``ChannelAdapter.send_text``.
    """

    message_id: str
    sender: str
    text: str


@runtime_checkable
class TextSender(Protocol):
    """The outbound half on its own (e.g. ``integration.whatsapp_client``)."""

    async def send_text(self, to: str, body: str) -> None: ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """Authenticate, normalize, send -- one channel's whole contract.

    ``verify_inbound`` runs on the raw, size-bounded body before anything is
    parsed or persisted, and rejects by raising ``fastapi.HTTPException``
    (normally 403). ``read_inbound`` runs only after it. Neither may touch
    the database: acceptance is the platform's.

    ``extract_turn`` returns ``None`` for a message the platform should
    complete without a turn (a non-text message, say). ``send_text`` raises
    on a failed send; the worker retries it (delivery is at least once).
    """

    def verify_inbound(self, body: bytes, headers: Headers) -> None: ...

    def read_inbound(self, body: bytes) -> InboundDelivery: ...

    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None: ...

    async def send_text(self, to: str, body: str) -> None: ...


@runtime_checkable
class SubscriptionHandshake(Protocol):
    """Optional: a channel whose provider verifies the URL with a GET.

    Meta calls ``GET /webhook`` with a challenge to echo; most channels
    (Telegram's ``setWebhook``, for one) have no such step, and the route
    answers 405 for them.
    """

    def verify_subscription(self, params: Mapping[str, str]) -> str: ...


@dataclass(frozen=True)
class Channel:
    """One channel a deployment registers: ``create_app(channels={name: ...})``.

    ``runtime_id`` names the registered runtime (``create_app(agents=...)``
    or ``AGENT_REGISTRATIONS``) that answers this channel. Boot fails if it
    is not registered, if it resolves ``untrusted_input=False`` (ADR-002
    C.13), or if its execution budget does not fit the outbox lease -- the
    same checks ``WHATSAPP_RUNTIME_ID`` gets.
    """

    adapter: ChannelAdapter
    runtime_id: str


#: Builds a channel's inbound adapter from the request's ``Settings``. The
#: built-in WhatsApp channel reads its secret and verify token per request
#: this way (so a dependency override reaches it); a registered channel
#: ignores the argument and returns its own adapter.
ChannelResolver = Callable[["Settings"], ChannelAdapter]


@dataclass(frozen=True)
class InboundChannels:
    """What the generic webhook route resolves a channel from.

    ``default`` is the channel ``/webhook`` (no channel segment) serves --
    the URL Meta is already configured to call.
    """

    default: str
    resolvers: Mapping[str, ChannelResolver]

    def resolve(self, name: str, settings: Settings) -> ChannelAdapter | None:
        resolver = self.resolvers.get(name)
        return None if resolver is None else resolver(settings)
