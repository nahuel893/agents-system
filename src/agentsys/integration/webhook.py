"""FastAPI router for Meta WhatsApp Cloud API webhook endpoint."""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from starlette.responses import PlainTextResponse

from agentsys.config import Settings, get_settings
from agentsys.integration.meta_signature import verify_signature
from agentsys.models.base import get_session_factory
from agentsys.services.clients import lookup_or_create_client, normalize_phone
from agentsys.services.conversation_log import log_conversation_turn
from agentsys.services.dedup import is_duplicate
from agentsys.services.redis import get_redis_client

webhook_router = APIRouter(prefix="/webhook", tags=["webhook"])

logger = structlog.get_logger()


def _extract_assistant_text(messages: list[AnyMessage]) -> str:
    """Extract the final assistant text from a run_turn result list.

    Mirrors openai_adapter._extract_assistant_text — handles both str and
    list-of-blocks content, skips intermediate tool-call AIMessages.
    """
    final_text = ""
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        if msg.tool_calls:
            continue
        content = msg.content
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        if content:
            final_text = str(content)
    return final_text


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

    # Client lookup — normalize phone and find or create client
    phone = normalize_phone(phone_number)
    client_record = None
    try:
        session_factory = get_session_factory(request.app.state.engine)
        async with session_factory() as session:
            client_record = await lookup_or_create_client(session, phone)
    except Exception:
        logger.warning("client_lookup.db_error", phone_number=phone)

    if client_record is not None and not client_record.active:
        logger.info(
            "webhook.unregistered_client",
            phone_number=phone,
            client_id=client_record.id,
        )
        return {"status": "ok"}

    logger.info(
        "webhook.message_received",
        phone_number=phone,
        message_id=message_id,
        text=text,
        timestamp=timestamp,
    )

    # Resolve the cached runtime for this deployment (D-012 app.state.runtimes,
    # no per-request build). Unknown/unresolved id → log + 200, no run_turn/send.
    runtimes: dict[str, Any] = getattr(request.app.state, "runtimes", {})
    runtime = runtimes.get(settings.whatsapp_runtime_id)
    if runtime is None:
        logger.warning(
            "webhook.runtime_unresolved",
            whatsapp_runtime_id=settings.whatsapp_runtime_id,
        )
        return {"status": "ok"}

    # Invoke the agent turn. permissions default to the runtime's own grants
    # (design AD-4) — no forced empty tuple (discovery #184).
    # Guarded: dedup already marked this message_id in Redis above, so a
    # crash here would silently drop the customer's message forever (Meta's
    # retry gets absorbed by dedup) — the agent turn MUST never propagate.
    try:
        result_messages = await runtime.run_turn(
            messages=[HumanMessage(content=text)],
            session_id=message_id,
            # D-014 S4 (design AD-1/AD-7): thread_id opts THIS call into the
            # shared checkpointer — only when the operator has enabled it.
            # whatsapp_checkpointer_enabled=False is a deliberate CONFIGURED
            # stateless mode (no thread_id passed, no degradation logging),
            # distinct from AD-8's unplanned runtime-failure degradation.
            thread_id=phone if settings.whatsapp_checkpointer_enabled else None,
        )
        assistant_text = _extract_assistant_text(result_messages)
    except Exception as exc:
        logger.warning(
            "webhook.run_turn_error",
            message_id=message_id,
            phone_number=phone,
            error=str(exc),
        )
        return {"status": "ok"}

    # Best-effort audit trail (design AD-6) — own session/transaction, never
    # blocks the reply. Order per spec: agent -> log -> send -> 200.
    try:
        log_session_factory = get_session_factory(request.app.state.engine)
        async with log_session_factory() as log_session:
            await log_conversation_turn(
                log_session,
                thread_id=phone,
                client_id=client_record.id if client_record is not None else None,
                user_text=text,
                assistant_text=assistant_text,
            )
            await log_session.commit()
    except Exception as exc:
        logger.warning(
            "conversation_log.write_error",
            phone_number=phone,
            error=str(exc),
        )

    # Best-effort outbound send — never let a Graph API failure crash the
    # webhook. Meta must always get a 200 (design AD-2). Skip entirely when
    # there is no text to send (limit/timeout terminals are non-empty by
    # design, but this guard stays defensive against an empty extraction).
    if assistant_text:
        try:
            await request.app.state.whatsapp_client.send_text(
                to=phone, body=assistant_text
            )
        except Exception as exc:
            logger.warning(
                "whatsapp.send_error",
                phone_number=phone,
                error=str(exc),
            )

    return {"status": "ok"}
