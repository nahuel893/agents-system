"""OpenAI-compatible adapter router (D-012).

Exposes:
  GET  /v1/models            — list of configured agent runtimes in OpenAI models-list shape
  POST /v1/chat/completions  — forward to AgentRuntime.run_turn; non-streaming only

Auth:
  If Settings.adapter_api_key is set, every /v1/* request must carry
  ``Authorization: Bearer <key>`` (constant-time comparison). If unset, the
  app logs a startup WARNING and the endpoint is open (dev-friendly default).

Surface:
  These endpoints serve ``app.state.adapter_model_ids``, NOT the whole
  ``app.state.runtimes`` cache. The two differ: the cache holds a runtime for
  every channel that needs one, and only the ids the operator named in
  ``adapter_runtimes`` belong on an HTTP surface whose authentication is
  optional. Serving the cache verbatim could publish a runtime from another
  channel on an unauthenticated /v1 surface.

  Absent means EMPTY, deliberately: an application that never declared an
  adapter surface exposes nothing rather than everything it happens to hold.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from contextlib import AsyncExitStack
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agents_system.config import get_settings
from agents_system.integration.body_limits import read_bounded_body

logger = structlog.get_logger()

_http_bearer = HTTPBearer(auto_error=False)

openai_router = APIRouter(prefix="/v1", tags=["openai-adapter"])


# ---------------------------------------------------------------------------
# Model-id helpers
# ---------------------------------------------------------------------------


def to_model_id(role: str, deployment: str | None) -> str:
    """Derive a stable, URL-safe model id from a (role, deployment) pair.

    Convention (design AD#3):
        <deployment>__<role>   →  e.g. "acme__sales-agent"
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


def _exposed_runtimes(request: Request) -> dict[str, Any]:
    """The runtimes this HTTP surface may serve.

    `app.state.runtimes` is the cache every channel draws from; this is the
    subset the operator asked to publish. Defaulting to an empty set when
    `adapter_model_ids` is unset is what keeps the invariant "a model on /v1
    implies the operator named it" true by construction.
    """
    cached: dict[str, Any] = getattr(request.app.state, "runtimes", {})
    exposed: frozenset[str] = getattr(
        request.app.state, "adapter_model_ids", frozenset()
    )
    return {name: rt for name, rt in cached.items() if name in exposed}


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


@openai_router.get("/models", dependencies=[Depends(verify_bearer)])
async def list_models(request: Request) -> dict[str, Any]:
    """Return the list of available agent runtimes in OpenAI models-list shape."""
    runtimes = _exposed_runtimes(request)
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
                block.get("text", "") for block in content if isinstance(block, dict)
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

    Body reading order: size ceiling -> raw bytes -> json.loads (#37). This
    route can be reached fully unauthenticated whenever adapter_api_key is
    unset (verify_bearer is a no-op dependency in that mode), so the size
    check has to run before any authentication-independent parsing -- never
    uses request.json() directly, which would buffer the whole body first.
    The ceiling itself is the shared ``read_bounded_body`` primitive
    (``body_limits.py``), the same one #140's webhook guard uses.
    """
    settings = get_settings()
    raw_body = await read_bounded_body(request, settings.adapter_max_body_bytes)
    body: dict[str, Any] = json.loads(raw_body)

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
    runtimes = _exposed_runtimes(request)

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
    #
    # #46 — this is a second turn-running entry point besides the webhook
    # worker, and each request runs one in-process, concurrently with every
    # other. It shares the SAME TurnAdmissionLimiter the lifespan built for
    # the worker (main.py::lifespan), so the process-wide bound on concurrent
    # turns holds across both. A request served outside that lifespan (e.g.
    # a test app that never runs it) simply runs unbound, exactly as before.
    #
    # SHOULD-FIX 2 (review follow-up): unlike the webhook worker (which
    # reserves its slot before claiming and so never waits with a lease
    # already running), this in-request caller has nothing else it could do
    # but wait -- and an unbounded wait would hang the HTTP request
    # indefinitely under a sustained burst. Bound it: on expiry, answer 503
    # with Retry-After rather than hang the client.
    admission_limiter = getattr(request.app.state, "turn_admission_limiter", None)
    turn_stack = AsyncExitStack()
    if admission_limiter is not None:
        wait_timeout_s = settings.admission_wait_timeout_s
        try:
            await asyncio.wait_for(
                turn_stack.enter_async_context(admission_limiter.slot()),
                timeout=wait_timeout_s,
            )
        except TimeoutError:
            raise HTTPException(
                status_code=503,
                detail="The agent is at capacity. Retry shortly.",
                headers={"Retry-After": str(max(1, int(wait_timeout_s)))},
            ) from None

    async with turn_stack:
        try:
            result_messages: list[AnyMessage] = await runtime.run_turn(
                messages=lc_messages,
                session_id=session_id,
            )
        except Exception:
            # Anything escaping run_turn used to leave here as a raw 500 with
            # nothing written anywhere: RequestIdMiddleware is try/finally with
            # no `except`, and this codebase has neither Sentry nor metrics, so
            # an unlogged 500 is an outage nobody can see. Log it with the
            # stack, and answer with a fixed message — an internal exception
            # string can carry a DSN, a prompt, or a customer's data, and this
            # response goes straight to the client.
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
