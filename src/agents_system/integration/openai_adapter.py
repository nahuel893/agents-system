"""OpenAI-compatible adapter router (D-012).

Exposes:
  GET  /v1/models            — list of configured agent runtimes in OpenAI models-list shape
  POST /v1/chat/completions  — forward to AgentRuntime.run_turn; non-streaming only
  GET  /v1/files/{token}     — download an output file (#319); mounted only
                               when create_app(output_files=...) is set, and
                               served without the bearer key (the token is
                               the capability)

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

from agents_system.extras import raise_missing_extra
from agents_system.subject import TurnActor, TurnSubject

try:
    from fastapi import APIRouter, Depends, HTTPException, Request, Response
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:
    raise_missing_extra("server", "The OpenAI-compatible adapter", exc)
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agents_system.config import get_settings
from agents_system.integration.body_limits import read_bounded_body
from agents_system.integration.output_files import (
    OutputFiles,
    append_to_content,
    content_disposition,
    render_output_files,
    strip_inline_images,
    validated_files,
)
from agents_system.integration.request_context import (
    DEFAULT_ADAPTER_CHANNEL,
    AdapterRequestInfo,
)

logger = structlog.get_logger()

_http_bearer = HTTPBearer(auto_error=False)

openai_router = APIRouter(prefix="/v1", tags=["openai-adapter"])
output_files_router = APIRouter(prefix="/v1", tags=["openai-adapter"])


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

    check_bearer(credentials, expected_key)


def check_bearer(
    credentials: HTTPAuthorizationCredentials | None, expected_key: str
) -> None:
    """Raise 401 unless ``credentials`` is a Bearer token equal to ``expected_key``.

    The one constant-time comparison every bearer-keyed surface shares: the
    adapter, and the operator routes with their own key (ADR-011 part 6).
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected Bearer token).",
        )

    if not secrets.compare_digest(
        credentials.credentials.encode(), expected_key.encode()
    ):
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
            # A client such as Open WebUI keeps a failed reply in its history
            # as an assistant message with null or empty content and resends
            # it on every later request. Mapping null raised and an empty one
            # reached the model, so one failed reply broke the conversation
            # for good. It carries nothing for the model: drop it.
            if not _has_text(content):
                continue
            result.append(AIMessage(content=content))
        # Unknown roles are silently skipped
    return result


def _has_text(content: Any) -> bool:
    """Whether OpenAI message *content* (a string or a list of parts) holds
    any non-blank text."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and part["text"].strip()
            for part in content
        )
    return False


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


async def _resolve_subject(request: Request) -> TurnSubject | None:
    """The turn subject for this request, or None when there is none."""
    resolver = getattr(request.app.state, "subject_resolver", None)
    if resolver is None:
        return None
    try:
        participant = await resolver.resolve(request)
    except Exception:
        structlog.get_logger(__name__).exception("adapter.subject_resolver_failed")
        raise HTTPException(
            status_code=500,
            detail="The request's end user could not be resolved. The failure has been logged.",
        ) from None
    if participant is None:
        return None
    if not participant.active:
        raise HTTPException(status_code=403, detail="This user is not active.")
    return TurnSubject.for_participant(participant.id, channel="api")


async def _request_context(request: Request, info: AdapterRequestInfo) -> str:
    """The trusted context the application's hook builds for this request.

    Issue #249. Empty when no hook is registered or the hook has nothing to
    say. Fails closed: a hook that raises, or returns anything but text or
    ``None``, answers a 500 and no turn runs, because a turn without the
    context the application asked for (its channel's format rules, say)
    would be a different turn, not a degraded one. The hook's own error never
    reaches the client: it can carry a DSN or a customer's data.
    """
    hook = getattr(request.app.state, "request_context_hook", None)
    if hook is None:
        return ""
    try:
        text = await hook(info)
        if text is not None and not isinstance(text, str):
            raise TypeError(
                f"request_context_hook returned {type(text).__name__}, "
                "expected str or None"
            )
    except Exception:
        structlog.get_logger(__name__).exception(
            "adapter.request_context_failed",
            model_id=info.runtime_id,
            session_id=info.session_id,
        )
        raise HTTPException(
            status_code=500,
            detail="The request context could not be built. The failure has been logged.",
        ) from None
    return text.strip() if text else ""


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
    output_files: OutputFiles | None = getattr(request.app.state, "output_files", None)
    if output_files is not None:
        # #319 -- the client resends the whole history, inline images the
        # adapter rendered included: keep their base64 away from the model.
        openai_messages = _without_inline_images(openai_messages)
    lc_messages = map_messages(openai_messages)

    # ADR-011: who this turn is for. Only a deployment-supplied resolver over
    # the authenticated request can name an end user; the body's `user` field
    # and message text are client-chosen and never a subject.
    subject = await _resolve_subject(request)
    actor = TurnActor(kind="api", id=model_id)

    # Call AgentRuntime.run_turn
    session_id = str(uuid.uuid4())

    # Issue #249 -- the application's per-request context, built from platform
    # metadata only (never the body), before the turn takes a capacity slot.
    # Passed explicitly into this request's turn, so concurrent requests can
    # never see each other's. No hook, or nothing to say, leaves the call
    # below exactly as it was.
    request_info = AdapterRequestInfo(
        runtime_id=model_id,
        channel=getattr(request.app.state, "adapter_channel", DEFAULT_ADAPTER_CHANNEL),
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        session_id=session_id,
        subject=subject,
    )
    trusted_context = await _request_context(request, request_info)
    context_kwargs: dict[str, str] = (
        {"trusted_context": trusted_context} if trusted_context else {}
    )
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
            # Review finding 5 (PR #87) -- `run_turn_with_usage` (not
            # `run_turn`) is the explicit, type-safe way to get `.usage`;
            # see `agent/graph.py`'s `TurnResult` docstring for why this
            # replaced the earlier `TurnMessages` list subclass.
            #
            # Review finding 4 (PR #87) -- deliberately NOT passing
            # `model_id=model_id` (the client's own registered runtime id)
            # here anymore: that id names WHICH runtime to call,
            # not which provider model bills the tokens, and using it as the
            # Settings.model_prices key broke pricing for every other entry
            # point keyed differently (the eval pipeline) and left the
            # WhatsApp webhook worker's turns permanently unpriced (it never
            # had a routing id like this to pass). Omitting it here lets
            # `AgentRuntime` fall back to its own derived default --
            # `model_display_name(model)`, the actual provider model id --
            # so every entry point prices under the same, correct key.
            turn_result = await runtime.run_turn_with_usage(
                messages=lc_messages,
                session_id=session_id,
                subject=subject,
                actor=actor,
                **context_kwargs,
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
    assistant_text = _extract_assistant_text(turn_result.messages)
    if output_files is not None:
        assistant_text = await _with_output_files(
            request, output_files, request_info, turn_result.messages, assistant_text
        )

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
        # #78 Phase 0 — real usage from the provider's own AIMessage.usage_metadata
        # (agent/graph.py's TurnUsage), replacing the old hardcoded zeros.
        # Honesty rule: `run_turn_with_usage` returns TurnUsage.total_tokens
        # (and the other counts) as None whenever any of this turn's model
        # calls reported no usage_metadata. Review finding 1 (PR #87):
        # `_usage_payload` turns that into a top-level `"usage": null`, never
        # an object with null fields (the official openai SDK's
        # CompletionUsage requires non-Optional ints).
        "usage": _usage_payload(turn_result.usage),
    }


async def _with_output_files(
    request: Request,
    config: OutputFiles,
    info: AdapterRequestInfo,
    messages: list[AnyMessage],
    text: str,
) -> str:
    """``text`` followed by the files the application's hook returns (#319).

    Called after the turn, outside its capacity slot, with this request's
    own ``info`` and messages, so concurrent requests never see each other's
    files. Unlike the request-context hook, a failure here does not fail the
    request: the turn already ran and its cost is spent, so the reply carries
    the text alone and the failure is logged. The hook's error never reaches
    the client.
    """
    log_fields = {"model_id": info.runtime_id, "session_id": info.session_id}
    try:
        files = validated_files(await config.hook(info, messages))
    except Exception:
        structlog.get_logger(__name__).exception(
            "adapter.output_files_failed", **log_fields
        )
        return text
    if not files:
        return text
    base_url = config.public_base_url or str(request.base_url).rstrip("/")
    parts = await render_output_files(
        config, files, base_url=base_url, log_fields=log_fields
    )
    return append_to_content(text, parts)


def _without_inline_images(
    openai_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assistant messages with every inline ``data:`` image replaced by its
    label; user messages untouched."""
    result: list[dict[str, Any]] = []
    for msg in openai_messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            result.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str):
            result.append({**msg, "content": strip_inline_images(content)})
        elif isinstance(content, list):
            parts = [
                {**part, "text": strip_inline_images(part["text"])}
                if isinstance(part, dict) and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
            result.append({**msg, "content": parts})
        else:
            result.append(msg)
    return result


# ---------------------------------------------------------------------------
# GET /v1/files/{token} -- #319, mounted only when output files are configured
# ---------------------------------------------------------------------------

# FastAPI's own answer for a path that matches no route, so an unknown,
# expired or malformed token is indistinguishable from a missing route.
_FILE_NOT_FOUND = "Not Found"


@output_files_router.get("/files/{token}")
async def download_output_file(token: str, request: Request) -> Response:
    """Serve one output file by its token, with no bearer key.

    The token is the only authority: unguessable, short-lived, stored as a
    hash. There is no listing route. Every miss answers the same 404.
    """
    config: OutputFiles | None = getattr(request.app.state, "output_files", None)
    if config is None:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)
    try:
        stored = await config.store.get(token)
    except Exception:
        structlog.get_logger(__name__).exception("adapter.output_file_read_failed")
        stored = None
    if stored is None:
        raise HTTPException(status_code=404, detail=_FILE_NOT_FOUND)
    return Response(
        content=stored.data,
        media_type=stored.mime_type,
        headers={
            "Content-Disposition": content_disposition(stored.filename),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Referrer-Policy": "no-referrer",
        },
    )


def _usage_payload(usage: Any) -> dict[str, int] | None:
    """Build the OpenAI-shaped `usage` object from a `TurnUsage` (or `None`
    when the runtime reported none at all) -- see the honesty note above.

    Review finding 1 (PR #87) -- returns `None` (JSON `null`) for the WHOLE
    object whenever any field is unknown, never an object with `null`
    fields. The official `openai` SDK's `CompletionUsage` requires
    non-Optional ints for every field, while `ChatCompletion.usage` itself
    is `Optional[CompletionUsage]` -- so `{"prompt_tokens": null, ...}`
    crashes client-side Pydantic validation, but a top-level `"usage": null`
    deserializes cleanly as `response.usage is None`. Verified against the
    real SDK in `tests/test_openai_adapter.py`
    (`openai.types.chat.ChatCompletion.model_validate`).
    """
    if (
        usage is None
        or usage.input_tokens is None
        or usage.output_tokens is None
        or usage.total_tokens is None
    ):
        return None
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }
