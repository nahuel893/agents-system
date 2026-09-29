"""The generic inbound webhook route: one channel-agnostic path (#22).

``POST /webhook/{channel}`` resolves the channel's adapter
(``agents_system.channels.port.ChannelAdapter``) and does only what is
common to every channel: bound the body, let the adapter authenticate and
normalize it, and durably persist each message it names before
acknowledging. ``/webhook`` with no channel segment serves the default
channel -- WhatsApp, the URL Meta is already configured to call -- with the
exact pre-#22 behaviour. The route itself knows nothing about any channel:
signature schemes, payload shapes and outbound sends all live in adapters.
"""

from __future__ import annotations

import structlog

from agents_system.extras import raise_missing_extra

try:
    from fastapi import APIRouter, Depends, HTTPException, Request
    from starlette.responses import PlainTextResponse
except ImportError as exc:
    raise_missing_extra("server", "The WhatsApp webhook", exc)

from agents_system.channels.port import (
    ChannelAdapter,
    InboundChannels,
    SubscriptionHandshake,
)
from agents_system.config import Settings, get_settings
from agents_system.integration.body_limits import read_bounded_body
from agents_system.models.base import get_session_factory
from agents_system.services.outbox import accept_inbound_message

webhook_router = APIRouter(prefix="/webhook", tags=["webhook"])

logger = structlog.get_logger()


def _inbound_channels(request: Request) -> InboundChannels:
    channels = getattr(request.app.state, "inbound_channels", None)
    if not isinstance(channels, InboundChannels):
        # Every app is built by create_app, which always sets this; without
        # it no channel is reachable, which is the safe reading.
        raise HTTPException(status_code=404, detail="Unknown channel")
    return channels


def _resolve_adapter(
    request: Request, channel: str | None, settings: Settings
) -> tuple[str, ChannelAdapter]:
    """The channel's name and its inbound adapter, or 404.

    Resolved before the body is read: an unknown channel costs nothing.
    """
    channels = _inbound_channels(request)
    name = channels.default if channel is None else channel
    adapter = channels.resolve(name, settings)
    if adapter is None:
        raise HTTPException(status_code=404, detail="Unknown channel")
    return name, adapter


def _verify_subscription(
    request: Request, channel: str | None, settings: Settings
) -> str:
    _, adapter = _resolve_adapter(request, channel, settings)
    if not isinstance(adapter, SubscriptionHandshake):
        raise HTTPException(
            status_code=405, detail="Channel has no subscription handshake"
        )
    return adapter.verify_subscription(request.query_params)


@webhook_router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """The default channel's GET handshake (Meta's verify-token challenge)."""
    return _verify_subscription(request, None, settings)


@webhook_router.get("/{channel}", response_class=PlainTextResponse)
async def verify_channel_webhook(
    channel: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """A named channel's GET handshake; 405 for a channel that has none."""
    return _verify_subscription(request, channel, settings)


@webhook_router.post("")
async def receive_message(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """The default channel's inbound delivery (Meta WhatsApp)."""
    return await _receive(request, None, settings)


@webhook_router.post("/{channel}")
async def receive_channel_message(
    channel: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """A named channel's inbound delivery."""
    return await _receive(request, channel, settings)


async def _receive(
    request: Request, channel: str | None, settings: Settings
) -> dict[str, str]:
    """Durably persist every valid message of a delivery, then acknowledge.

    Order: resolve the adapter -> size ceiling -> raw bytes -> the adapter's
    ``verify_inbound`` -> its ``read_inbound``. Nothing is parsed before
    the adapter has authenticated the raw body, and nothing is persisted
    before that either. An oversized body (#140) is rejected with 413 before
    authentication even runs; the ceiling is the shared
    ``read_bounded_body`` primitive (#37, ``body_limits.py``).

    Once a message is accepted here, everything else -- the agent turn and
    the outbound send -- happens entirely OUT of this request, in
    ``DeferredWebhookWorker``. This handler never runs a turn and never
    sends anything.

    A delivery may carry several messages. Each is persisted in delivery
    order; the first one whose commit fails aborts the request with 503 so
    the provider retries the whole delivery. Any message already committed
    before that failure is accepted again idempotently on that retry -- its
    channel-scoped id is the durable inbox's uniqueness key
    (``accept_inbound_message``), not this handler's job to deduplicate.
    """
    name, adapter = _resolve_adapter(request, channel, settings)
    body = await read_bounded_body(request, settings.webhook_max_body_bytes)
    adapter.verify_inbound(body, request.headers)

    delivery = adapter.read_inbound(body)
    if not delivery.messages:
        return {"status": "ok"}

    session_factory = get_session_factory(request.app.state.engine)
    for message in delivery.messages:
        try:
            async with session_factory() as session:
                await accept_inbound_message(
                    session,
                    meta_message_id=message.message_id,
                    payload=delivery.payload,
                    conversation_key=message.conversation_key,
                    channel=name,
                )
        except Exception as exc:
            logger.error(
                "webhook.persist_failed",
                message_id=message.message_id,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503,
                detail="failed to durably persist an inbound message",
            ) from exc

    return {"status": "ok"}
