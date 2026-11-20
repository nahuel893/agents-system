"""Bearer authentication of the metrics endpoint."""

from __future__ import annotations

import secrets
from typing import Annotated

from agents_system.extras import raise_missing_extra

try:
    from fastapi import Depends, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:
    raise_missing_extra("server", "The served app", exc)

from agents_system.config import get_settings

#: #78 Phase 0 Slice 2 -- same HTTPBearer mechanism as
#: integration/openai_adapter.py's `_http_bearer` (a distinct instance: the
#: two endpoints are protected by two independent keys/settings fields).
_metrics_http_bearer = HTTPBearer(auto_error=False)


async def verify_metrics_access(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_metrics_http_bearer)
    ] = None,
) -> None:
    """FastAPI dependency gating `GET /metrics` (#78 Phase 0 Slice 2).

    Follows `integration/openai_adapter.py`'s `verify_bearer` fail-closed
    spirit, with one extra step in front of it (ADR-005 section 6 / issue
    #78's scope note: "unauthenticated exposure only under an explicit
    setting"):

    - `metrics_enabled` is `False` (the default): 404 -- the endpoint does
      not exist. Absent, not merely unauthenticated, so probing for it
      reveals nothing about whether metrics are configured at all.
    - `metrics_enabled` is `True` + `metrics_api_key` set: Bearer auth
      enforced (constant-time compare), identical shape to `verify_bearer`.
    - `metrics_enabled` is `True` + `metrics_api_key` empty: open. Refused
      at boot by `Settings.validate_security_fail_closed` unless
      `allow_insecure=True` was also set explicitly (dev-only escape hatch,
      same as the adapter's own open mode).
    """
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404)

    expected_key = settings.metrics_api_key
    if not expected_key:
        # Open mode — Settings itself already refused to boot this way
        # without allow_insecure=True.
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected Bearer token).",
        )

    if not secrets.compare_digest(credentials.credentials, expected_key):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")
