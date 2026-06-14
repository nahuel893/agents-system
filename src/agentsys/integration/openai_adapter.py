"""OpenAI-compatible adapter router (D-012).

Exposes:
  GET  /v1/models            — list of configured agent runtimes in OpenAI models-list shape
  POST /v1/chat/completions  — (slice 2) forward to AgentRuntime.run_turn

Auth:
  If Settings.adapter_api_key is set, every /v1/* request must carry
  ``Authorization: Bearer <key>`` (constant-time comparison). If unset, the
  app logs a startup WARNING and the endpoint is open (dev-friendly default).
"""
from __future__ import annotations

import secrets
import time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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
