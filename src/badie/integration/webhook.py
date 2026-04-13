"""FastAPI router for Meta WhatsApp Cloud API webhook endpoint."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import PlainTextResponse

from badie.config import Settings, get_settings
from badie.integration.meta_signature import verify_signature
from badie.services.dedup import is_duplicate
from badie.services.redis import get_redis_client

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


@webhook_router.post("")
async def receive_message(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Receive and process incoming Meta webhook events.

    Body reading order: raw bytes → HMAC verify → json.loads.
    Never uses request.json() before HMAC verification.
    """
    body = await request.body()
    verify_signature(body, request.headers, settings.meta_webhook_secret)

    payload = json.loads(body)

    # Navigate to value: entry[0].changes[0].value
    try:
        value = payload["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError):
        return {"status": "ok"}

    messages = value.get("messages")
    if not messages:
        # Status update or other non-message event — silently discard
        return {"status": "ok"}

    # Extract fields from the first message
    message = messages[0]
    phone_number = message.get("from", "")
    message_id = message.get("id", "")
    text = message.get("text", {}).get("body", "")
    timestamp = message.get("timestamp", "")

    # Dedup check — skip if already processed within TTL window
    redis_client = get_redis_client(settings.redis_url)
    if await is_duplicate(redis_client, message_id):
        logger.info("webhook.duplicate_skipped", message_id=message_id)
        return {"status": "ok"}

    logger.info(
        "webhook.message_received",
        phone_number=phone_number,
        message_id=message_id,
        text=text,
        timestamp=timestamp,
    )

    return {"status": "ok"}
