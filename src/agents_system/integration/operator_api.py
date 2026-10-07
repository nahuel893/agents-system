"""Operator routes: export or delete one end user's data (ADR-011 part 6, #257).

``POST /operator/subjects/export`` and ``POST /operator/subjects/delete`` take
``{runtime_id, subject_key, thread_ids?}`` in a JSON body, so the subject key
never lands in an access log. They cover every store in
``app.state.subject_data_registry`` and the subject's checkpointer threads:
the ids the deployment's ``subject_threads`` hook returns plus any the
operator names.

They authenticate with ``operator_api_key``, compared with the adapter's
constant-time check, and ``create_app`` mounts them only when that key is
set: without it the routes do not exist.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import structlog
from pydantic import BaseModel, Field

from agents_system.config import get_settings
from agents_system.extras import raise_missing_extra
from agents_system.integration.openai_adapter import check_bearer
from agents_system.subject import TurnActor
from agents_system.subject_data import (
    DEFAULT_REGISTRY,
    SubjectDeletionError,
    SubjectRef,
    delete_subject,
    export_subject,
)

try:
    from fastapi import APIRouter, Depends, Header, HTTPException, Request
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:
    raise_missing_extra("server", "The operator routes", exc)

logger = structlog.get_logger(__name__)

_http_bearer = HTTPBearer(auto_error=False)
_OPERATOR_ID = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

SubjectThreads = Callable[[SubjectRef], Awaitable[list[str]]]


async def verify_operator(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_http_bearer)
    ] = None,
) -> None:
    expected = get_settings().operator_api_key
    if not expected:
        raise HTTPException(status_code=404)
    check_bearer(credentials, expected)


class SubjectRequest(BaseModel):
    runtime_id: str = Field(min_length=1, max_length=200)
    subject_key: str = Field(min_length=1, max_length=200)
    thread_ids: list[str] = Field(default_factory=list, max_length=100)


router = APIRouter(
    prefix="/operator/subjects",
    tags=["operator"],
    dependencies=[Depends(verify_operator)],
)


async def _thread_ids(request: Request, ref: SubjectRef, named: list[str]) -> list[str]:
    hook: SubjectThreads | None = getattr(request.app.state, "subject_threads", None)
    found = await hook(ref) if hook is not None else []
    return list(dict.fromkeys([*found, *named]))


def _actor(operator_id: str | None) -> TurnActor:
    if operator_id is not None and not _OPERATOR_ID.match(operator_id):
        raise HTTPException(status_code=422, detail="Invalid X-Operator-Id.")
    return TurnActor(kind="operator", id=operator_id or "operator")


@router.post("/export")
async def export_route(body: SubjectRequest, request: Request) -> dict[str, Any]:
    ref = SubjectRef(body.runtime_id, body.subject_key)
    state = request.app.state
    return await export_subject(
        getattr(state, "subject_data_registry", DEFAULT_REGISTRY),
        ref,
        threads=getattr(state, "checkpoint_threads", None),
        thread_ids=await _thread_ids(request, ref, body.thread_ids),
    )


@router.post("/delete")
async def delete_route(
    body: SubjectRequest,
    request: Request,
    x_operator_id: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    ref = SubjectRef(body.runtime_id, body.subject_key)
    state = request.app.state
    try:
        return await delete_subject(
            getattr(state, "subject_data_registry", DEFAULT_REGISTRY),
            ref,
            actor=_actor(x_operator_id),
            threads=getattr(state, "checkpoint_threads", None),
            thread_ids=await _thread_ids(request, ref, body.thread_ids),
        )
    except SubjectDeletionError as exc:
        raise HTTPException(
            status_code=500,
            detail={"failed": exc.failed, "deleted": exc.deleted},
        ) from exc
