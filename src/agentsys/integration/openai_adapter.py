"""OpenAI-compatible adapter router (D-012).

Exposes:
  GET  /v1/models            — list of configured agent runtimes in OpenAI models-list shape
  POST /v1/chat/completions  — forward to AgentRuntime.run_turn; non-streaming only

Auth:
  If Settings.adapter_api_key is set, every /v1/* request must carry
  ``Authorization: Bearer <key>`` (constant-time comparison). If unset, the
  app logs a startup WARNING and the endpoint is open (dev-friendly default).
"""
from __future__ import annotations

import secrets
import time
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agentsys.config import get_settings

logger = structlog.get_logger()

_http_bearer = HTTPBearer(auto_error=False)

openai_router = APIRouter(prefix="/v1", tags=["openai-adapter"])


# ---------------------------------------------------------------------------
# Model-id helpers
# ---------------------------------------------------------------------------


def to_model_id(role: str, deployment: str | None) -> str:
    """Derive a stable, URL-safe model id from a (role, deployment) pair.

    Convention (design AD#3):
        <deployment>__<role>   →  e.g. "badie__sales-agent"
        _generic__<role>       →  when deployment is None
    """
    prefix = deployment if deployment is not None else "_generic"
    return f"{prefix}__{role}"


def parse_model_id(model_id: str) -> tuple[str | None, str]:
    """Inverse of ``to_model_id``.

    Returns (deployment, role). deployment is None when the prefix is "_generic".
    """
    if "__" not in model_id:
        raise ValueError(f"Invalid model id (missing '__' separator): {model_id!r}")
    prefix, role = model_id.split("__", 1)
    deployment: str | None = None if prefix == "_generic" else prefix
    return deployment, role


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def verify_bearer(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_http_bearer)
    ] = None,
) -> None:
    """FastAPI dependency that enforces Bearer auth when adapter_api_key is set.

    - key unset → open, no-op (but the app warns at startup).
    - key set + no/wrong token → 401.
    - key set + correct token → proceeds.
    """
    settings = get_settings()
    expected_key = settings.adapter_api_key

    if not expected_key:
        # Open mode — already warned at startup
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected Bearer token).",
        )

    if not secrets.compare_digest(credentials.credentials, expected_key):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@openai_router.get("/models", dependencies=[Depends(verify_bearer)])
async def list_models(request: Request) -> dict[str, Any]:
    """Return the list of available agent runtimes in OpenAI models-list shape."""
    runtimes: dict[str, Any] = getattr(request.app.state, "runtimes", {})
    now = int(time.time())
    data = [
        {
            "id": model_id,
            "object": "model",
            "created": now,
            "owned_by": "agents-system",
        }
        for model_id in runtimes
    ]
    return {"object": "list", "data": data}


# ---------------------------------------------------------------------------
# Message mapping helpers
# ---------------------------------------------------------------------------


def map_messages(openai_messages: list[dict[str, Any]]) -> list[AnyMessage]:
    """Convert OpenAI message list to LangChain messages.

    Design decision AD#6: the runtime's own system prompt is authoritative.
    Client ``system`` messages are DROPPED — this is the privilege-escalation
    guard. ``_call_model`` (graph.py) prepends the runtime system prompt to
    the model input at call time (D-014 S4, design AD-1) — it is never part
    of this mapped list or of persisted state.
    """
    result: list[AnyMessage] = []
    for msg in openai_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            # Drop client system messages — design AD#6
            continue
        elif role == "user":
            result.append(HumanMessage(content=content))
        elif role == "assistant":
            result.append(AIMessage(content=content))
        # Unknown roles are silently skipped
    return result


def _extract_assistant_text(messages: list[AnyMessage]) -> str:
    """Extract the final assistant text from the run_turn result list.

    Handles both str and list-of-blocks content (mirrors scripts/chat.py:103-107).
    """
    final_text = ""
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        # Skip intermediate messages that are tool calls (not final reply)
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


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------


@openai_router.post("/chat/completions", dependencies=[Depends(verify_bearer)])
async def chat_completions(request: Request) -> dict[str, Any]:
    """Forward a chat-completion request to the resolved AgentRuntime.

    Design decisions:
      AD#1 — stream:true → reject with HTTP 400 (honesty over silent coerce)
      AD#6 — client system messages are dropped; runtime prompt wins
    """
    body: dict[str, Any] = await request.json()

    # AD#1: reject streaming requests
    if body.get("stream", False):
        raise HTTPException(
            status_code=400,
            detail=(
                "Streaming (stream:true) is not supported by this adapter. "
                "Set stream:false or omit the field."
            ),
        )

    model_id: str = body.get("model", "")
    runtimes: dict[str, Any] = getattr(request.app.state, "runtimes", {})

    # Resolve model id → cached runtime (404 if unknown)
    runtime = runtimes.get(model_id)
    if runtime is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_id}' not found. Call GET /v1/models for available ids.",
        )

    # Map messages (client system dropped — AD#6)
    openai_messages: list[dict[str, Any]] = body.get("messages", [])
    lc_messages = map_messages(openai_messages)

    # Call AgentRuntime.run_turn
    session_id = str(uuid.uuid4())
    # Permissions: omitted — run_turn defaults to the runtime's own resolved
    # grants (design AD-4). The adapter has no separate caller identity, so
    # the role's own permissions ARE the correct execution-time RBAC set.
    try:
        result_messages: list[AnyMessage] = await runtime.run_turn(
            messages=lc_messages,
            session_id=session_id,
        )
    except Exception:
        # Anything escaping run_turn used to leave here as a raw 500 with
        # nothing written anywhere: RequestIdMiddleware is try/finally with no
        # `except`, and this codebase has neither Sentry nor metrics, so an
        # unlogged 500 is an outage nobody can see. Log it with the stack, and
        # answer with a fixed message — an internal exception string can carry
        # a DSN, a prompt, or a customer's data, and this response goes
        # straight to the client.
        structlog.get_logger(__name__).exception(
            "adapter.turn_failed", model_id=model_id, session_id=session_id
        )
        raise HTTPException(
            status_code=500,
            detail="The agent could not complete this turn. The failure has been logged.",
        ) from None

    # Extract final assistant text
    assistant_text = _extract_assistant_text(result_messages)

    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": now,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": assistant_text},
                "finish_reason": "stop",
            }
        ],
        # Token usage is zeros for MVP — Open WebUI tolerates this (design open question)
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
