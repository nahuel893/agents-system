"""FastAPI router for Meta WhatsApp Cloud API webhook endpoint."""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import PlainTextResponse

from agents_system.config import Settings, get_settings
from agents_system.integration.body_limits import read_bounded_body
from agents_system.integration.meta_signature import verify_signature
from agents_system.models.base import get_session_factory
from agents_system.services.outbox import accept_inbound_message

webhook_router = APIRouter(prefix="/webhook", tags=["webhook"])

logger = structlog.get_logger()


@webhook_router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """Handle Meta's webhook verification handshake (GET challenge)."""
    params = request.query_params
    hub_mode = params.get("hub.mode")
    hub_verify_token = params.get("hub.verify_token")
    hub_challenge = params.get("hub.challenge")

    if not hub_challenge:
        raise HTTPException(status_code=400, detail="Missing hub.challenge")

    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return hub_challenge

    raise HTTPException(status_code=403, detail="Verification token mismatch")


def _extract_meta_messages(payload: Any) -> list[tuple[str, str]]:
    """Collect every valid Meta message in a (possibly batched) payload.

    Meta may deliver several messages sharing one signed envelope. Each gets
    its own durable inbox/outbox row keyed by its OWN Meta message id, and
    the FULL envelope is stored against every one of them so the deferred
    worker can later find the exact message that id refers to inside the
    shared batch (``webhook_worker._extract_inbound_turn``).

    Returns ``(message_id, conversation_key)`` pairs. ``conversation_key`` is
    the message's raw ``from`` (#46 follow-up: what the claim query
    serializes on, see ``services.outbox.pending_outbox_statement``) --
    unnormalized, since this route deliberately never touches the
    participant directory (that stays entirely in ``DeferredWebhookWorker``).
    A message with no usable ``from`` gets a key derived from its own id
    instead of an empty/shared one, so it can never collide with, or block,
    any other conversation.

    Returns an empty list for a status update, an absent/empty messages
    array, a malformed payload shape, or a message with no usable id -- none
    of those are persisted, and none of them make this raise.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []

    messages_out: list[tuple[str, str]] = []
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
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    continue
                sender = message.get("from")
                conversation_key = (
                    sender
                    if isinstance(sender, str) and sender
                    else f"unresolved:{message_id}"
                )
                messages_out.append((message_id, conversation_key))
    return messages_out


@webhook_router.post("")
async def receive_message(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Durably persist every valid Meta message, then acknowledge.

    Body reading order: size ceiling -> raw bytes -> HMAC verify ->
    json.loads. Never uses request.json() before HMAC verification, and
    never persists anything before it. An oversized body (#140) is rejected
    with 413 before HMAC verification even runs. The size ceiling itself is
    the shared ``read_bounded_body`` primitive (#37, ``body_limits.py``),
    not a copy local to this route.

    Once a message is accepted here, everything else -- the agent turn and
    the outbound send -- happens entirely OUT of this request, in
    ``DeferredWebhookWorker``. This handler never runs a turn and never
    calls the WhatsApp client.

    A batch may carry several messages under one signature. Each is
    persisted in delivery order; the first one whose commit fails aborts the
    request with 503 so Meta retries the whole envelope. Any message already
    committed before that failure is accepted again idempotently on that
    retry -- its Meta message id is the durable inbox's uniqueness key
    (``accept_inbound_message``), not this handler's job to deduplicate.
    """
    body = await read_bounded_body(request, settings.webhook_max_body_bytes)
    verify_signature(body, request.headers, settings.meta_webhook_secret)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"status": "ok"}

    messages = _extract_meta_messages(payload)
    if not messages:
        return {"status": "ok"}

    session_factory = get_session_factory(request.app.state.engine)
    for message_id, conversation_key in messages:
        try:
            async with session_factory() as session:
                await accept_inbound_message(
                    session,
                    meta_message_id=message_id,
                    payload=payload,
                    conversation_key=conversation_key,
                )
        except Exception as exc:
            logger.error(
                "webhook.persist_failed",
                message_id=message_id,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503,
                detail="failed to durably persist an inbound message",
            ) from exc

    return {"status": "ok"}
