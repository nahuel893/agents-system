"""The WhatsApp/Meta adapter: the first channel behind the port (#22).

Everything Meta-specific lives here and nowhere else: the
``X-Hub-Signature-256`` HMAC (``integration.meta_signature``), the GET
verify-token handshake, the Cloud API envelope walk, and the outbound send
through ``integration.whatsapp_client.WhatsAppClient``. Its behaviour is the
pre-#22 route's and worker's, moved verbatim -- the unchanged
``tests/test_webhook.py`` and ``tests/test_webhook_worker.py`` pin it.

Its configuration is ``WhatsAppChannelSettings``, a view projected from the
platform ``Settings``: the environment variables (``META_WEBHOOK_SECRET``,
``WHATSAPP_*``) keep their names, but only this adapter and the lifespan's
WhatsApp wiring read them.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from agents_system.extras import raise_missing_extra

try:
    from fastapi import HTTPException
    from starlette.datastructures import Headers
except ImportError as exc:
    raise_missing_extra("server", "The WhatsApp channel", exc)

from agents_system.channels.port import (
    InboundDelivery,
    InboundRef,
    InboundTurn,
    TextSender,
)
from agents_system.integration.meta_signature import verify_signature
from agents_system.models.outbox import LEGACY_CHANNEL
from agents_system.proactive import ProviderRateLimited, ProviderRejected

if TYPE_CHECKING:
    from agents_system.config import Settings

logger = structlog.get_logger()

#: The built-in channel's name: ``/webhook`` and ``/webhook/whatsapp``. It is
#: the inbox's legacy channel -- rows accepted before #22 are WhatsApp rows.
WHATSAPP_CHANNEL: Final = LEGACY_CHANNEL


@dataclass(frozen=True)
class WhatsAppChannelSettings:
    """The WhatsApp slice of ``Settings`` -- all this channel reads."""

    app_secret: str
    verify_token: str
    access_token: str
    phone_number_id: str
    graph_api_url: str
    runtime_id: str
    checkpointer_enabled: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> WhatsAppChannelSettings:
        return cls(
            app_secret=settings.meta_webhook_secret,
            verify_token=settings.whatsapp_verify_token,
            access_token=settings.whatsapp_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            graph_api_url=settings.whatsapp_graph_api_url,
            runtime_id=settings.whatsapp_runtime_id,
            checkpointer_enabled=settings.whatsapp_checkpointer_enabled,
        )


class WhatsAppChannel:
    """Meta WhatsApp Cloud API behind ``ChannelAdapter``.

    The inbound route builds one per request from that request's
    ``Settings`` (``settings`` given, no ``sender``); the deferred worker
    holds one around the app's shared ``WhatsAppClient`` (``sender`` given).
    Either half used without its dependency raises instead of guessing.
    """

    def __init__(
        self,
        settings: WhatsAppChannelSettings | None = None,
        *,
        sender: TextSender | None = None,
    ) -> None:
        self._settings = settings
        self._sender = sender

    @classmethod
    def from_settings(cls, settings: Settings) -> WhatsAppChannel:
        """The inbound adapter for one request (a ``ChannelResolver``)."""
        return cls(WhatsAppChannelSettings.from_settings(settings))

    def _require_settings(self) -> WhatsAppChannelSettings:
        if self._settings is None:
            raise RuntimeError("WhatsAppChannel has no inbound settings")
        return self._settings

    # -- inbound: authenticate ---------------------------------------------

    def verify_inbound(self, body: bytes, headers: Headers) -> None:
        """Meta's ``X-Hub-Signature-256`` HMAC over the raw body (403)."""
        verify_signature(body, headers, self._require_settings().app_secret)

    def verify_subscription(self, params: Mapping[str, str]) -> str:
        """Meta's GET verification handshake: echo the challenge."""
        hub_mode = params.get("hub.mode")
        hub_verify_token = params.get("hub.verify_token")
        hub_challenge = params.get("hub.challenge")

        if not hub_challenge:
            raise HTTPException(status_code=400, detail="Missing hub.challenge")

        configured_token = self._require_settings().verify_token
        # An unset token is not a credential: without this, the empty string
        # would match an empty (or absent-as-empty) ``hub.verify_token``.
        if (
            configured_token
            and hub_mode == "subscribe"
            and hub_verify_token is not None
            and hmac.compare_digest(
                hub_verify_token.encode("utf-8"),
                configured_token.encode("utf-8"),
            )
        ):
            return hub_challenge

        raise HTTPException(status_code=403, detail="Verification token mismatch")

    # -- inbound: normalize ------------------------------------------------

    def read_inbound(self, body: bytes) -> InboundDelivery:
        """Every valid Meta message in a (possibly batched) envelope.

        A body that is not JSON acknowledges with nothing persisted, exactly
        as the pre-#22 route did (only ``JSONDecodeError`` is caught there
        too). The FULL envelope is stored against every message in it so the
        worker can later find the exact message an id refers to inside a
        shared batch (``extract_turn``).
        """
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return InboundDelivery(payload={}, messages=())
        messages = _extract_meta_messages(payload)
        if not messages:
            return InboundDelivery(payload={}, messages=())
        return InboundDelivery(payload=payload, messages=tuple(messages))

    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None:
        """Find this inbox row's exact Meta message across a shared envelope.

        A message whose Meta ``type`` is not ``"text"`` (image, audio,
        location, document, ...) is intentionally never serviceable here:
        this platform relays text to the agent, not type-specific media, and
        does not attempt one today. That is logged explicitly so it reads as
        a deliberate, durably-completed outcome rather than a silent drop;
        replying to a non-text message is a documented product follow-up,
        not a bug here.
        """
        entries = payload.get("entry")
        if not isinstance(entries, list):
            return None
        for message in _walk_messages(entries):
            if message.get("id") != message_id:
                continue
            phone_number = message.get("from")
            text_value = message.get("text")
            text = text_value.get("body") if isinstance(text_value, dict) else None
            if not isinstance(phone_number, str) or not isinstance(text, str):
                message_type = message.get("type")
                if isinstance(message_type, str) and message_type != "text":
                    logger.info(
                        "webhook_worker.non_text_message",
                        meta_message_id=message_id,
                        message_type=message_type,
                    )
                return None
            return InboundTurn(message_id=message_id, sender=phone_number, text=text)
        return None

    # -- outbound ------------------------------------------------------------

    async def send_text(self, to: str, body: str) -> None:
        """Send through the Graph API client (raises on a non-2xx)."""
        if self._sender is None:
            raise RuntimeError("WhatsAppChannel has no outbound sender")
        await self._sender.send_text(to=to, body=body)

    async def send_proactive(self, to: str, body: str) -> None:
        """Send with no inbound turn (#251), classifying the provider's answer.

        A 429 proves nothing was sent and becomes ``ProviderRateLimited``
        with the ``Retry-After`` delay; any other 4xx is a definitive
        ``ProviderRejected``. A 5xx or a transport error is re-raised as is:
        the message may have reached Meta, so the dispatcher must not retry.
        Outside the 24-hour customer-service window Meta accepts only
        approved templates; this sends plain text (see docs/platform/channel.md).
        """
        try:
            await self.send_text(to, body)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 429:
                raise ProviderRateLimited(
                    _retry_after(error.response.headers.get("Retry-After"))
                ) from error
            if 400 <= status < 500:
                raise ProviderRejected(f"provider answered {status}") from error
            raise


_DEFAULT_RETRY_AFTER_S = 60.0


def _retry_after(value: str | None) -> float:
    """Seconds from a ``Retry-After`` header; a default when absent or not seconds."""
    try:
        seconds = float(value) if value is not None else 0.0
    except ValueError:
        return _DEFAULT_RETRY_AFTER_S
    return seconds if seconds > 0 else _DEFAULT_RETRY_AFTER_S


def _walk_messages(entries: list[Any]) -> list[dict[str, Any]]:
    """Every dict at ``entry[].changes[].value.messages[]``, in order.

    Tolerates any malformed level (a non-list, a non-dict) by skipping it:
    this is attacker-controlled input and must never raise.
    """
    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            found.extend(message for message in messages if isinstance(message, dict))
    return found


def _extract_meta_messages(payload: Any) -> list[InboundRef]:
    """Collect every valid Meta message in a (possibly batched) payload.

    Meta may deliver several messages sharing one signed envelope. Each gets
    its own durable inbox/outbox row keyed by its OWN Meta message id.

    ``conversation_key`` is the message's raw ``from`` (#46 follow-up: what
    the claim query serializes on, see
    ``services.outbox.pending_outbox_statement``) -- unnormalized, since the
    route deliberately never touches the participant directory (that stays
    entirely in ``DeferredWebhookWorker``). A message with no usable
    ``from`` gets a key derived from its own id instead of an empty/shared
    one, so it can never collide with, or block, any other conversation.

    Returns an empty list for a status update, an absent/empty messages
    array, a malformed payload shape, or a message with no usable id --
    none of those are persisted, and none of them make this raise.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []

    refs: list[InboundRef] = []
    for message in _walk_messages(entries):
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        sender = message.get("from")
        conversation_key = (
            sender if isinstance(sender, str) and sender else f"unresolved:{message_id}"
        )
        refs.append(
            InboundRef(message_id=message_id, conversation_key=conversation_key)
        )
    return refs
