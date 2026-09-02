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
from agentsys.services.dedup import is_duplicate
from agentsys.services.participants import (
    ConversationRecorder,
    ParticipantDirectory,
)
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



def get_participant_directory(request: Request) -> ParticipantDirectory | None:
    """Return the directory the application wired onto ``app.state``, if any.

    Returns None rather than raising because FastAPI resolves dependencies
    before the handler runs, and this route's contract is "raw bytes → HMAC
    verify → json.loads". A dependency that raised on a missing directory
    would answer 500 to a forged, unsigned request — reporting a server
    misconfiguration to exactly the caller who should get a 403.

    The handler fails closed at the point of use instead, once the delivery
    has been proven authentic.
    """
    directory: ParticipantDirectory | None = getattr(
        request.app.state, "participant_directory", None
    )
    return directory


def get_conversation_recorder(request: Request) -> ConversationRecorder | None:
    """Return the recorder, or None when the application configured none.

    Optional where the directory is not: turn history is a best-effort audit
    trail the caller already swallows failures for, so a deployment that
    keeps none simply records nothing.
    """
    recorder: ConversationRecorder | None = getattr(
        request.app.state, "conversation_recorder", None
    )
    return recorder


@webhook_router.post("")
async def receive_message(
    request: Request,
    settings: Settings = Depends(get_settings),
    directory: ParticipantDirectory | None = Depends(get_participant_directory),
    recorder: ConversationRecorder | None = Depends(get_conversation_recorder),
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

    # Client lookup — normalize phone and find or create client. The `from`
    # field is Meta-controlled input: an unparseable value is dropped with a
    # 200 (AD-2), never a 5xx that would make Meta retry a poison message.
    # Fail closed on a misconfigured deployment: with no directory there is
    # no way to separate a participant this deployment serves from any
    # address that can reach the endpoint, so the turn must not run. The
    # check sits here, after HMAC verification, not in the dependency.
    if directory is None:
        logger.error(
            "webhook.no_participant_directory",
            message_id=message_id,
            detail=(
                "app.state.participant_directory is unset; the platform "
                "ships no default because it owns no identity schema"
            ),
        )
        return {"status": "ok"}

    try:
        phone = directory.normalize_address(phone_number)
    except Exception:
        # The port asks for ValueError, but a consumer's parser may raise
        # anything. Catching only ValueError would turn one bad `from` field
        # into an unhandled 500 and a poison message Meta retries forever —
        # the exact outcome the always-200 contract (AD-2) exists to prevent.
        logger.warning("webhook.invalid_address", phone_number=phone_number)
        return {"status": "ok"}
    client_record = None
    try:
        session_factory = get_session_factory(request.app.state.engine)
        async with session_factory() as session:
            client_record = await directory.resolve(session, phone)
    except Exception:
        # BLOCKER 2 — fail CLOSED on a swallowed lookup error. Return 200 to
        # Meta (design AD-2) but do NOT fall through to run_turn / outbound
        # send: an unverified address must not reach the agent or receive a
        # reply.
        logger.warning("client_lookup.db_error", phone_number=phone)
        return {"status": "ok"}

    # Three outcomes, and only one of them may continue.
    #
    # None means the directory does not know this address. That used to be
    # unreachable: the function this port replaced was `lookup_or_create`,
    # which created the row and so never answered None on success — which is
    # why the check below only handled "known but inactive". Under the port,
    # None is a documented answer, and treating it as "keep going" is the
    # same fail-open this route rejects one level up for a missing directory.
    if client_record is None:
        logger.info("webhook.unknown_participant", phone_number=phone)
        return {"status": "ok"}

    if not client_record.active:
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
    #
    # Guarded rather than left to the handler below: "no recorder configured"
    # is a deployment choice, and letting it arrive as an AttributeError
    # inside `except Exception` would log it every turn as a write error that
    # nobody can fix.
    if recorder is not None:
        try:
            log_session_factory = get_session_factory(request.app.state.engine)
            async with log_session_factory() as log_session:
                await recorder.record_turn(
                    log_session,
                    thread_id=phone,
                    participant_id=client_record.id,
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
