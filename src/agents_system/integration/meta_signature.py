"""HMAC-SHA256 signature verification for Meta WhatsApp Cloud API webhooks."""

from __future__ import annotations

import hashlib
import hmac

from agents_system.extras import raise_missing_extra

try:
    from fastapi import HTTPException
    from starlette.datastructures import Headers
except ImportError as exc:
    raise_missing_extra("server", "The Meta webhook signature check", exc)

_HEADER_NAME = "x-hub-signature-256"


def sign_payload(body: bytes, secret: str) -> str:
    """Compute the `X-Hub-Signature-256` header value Meta would send for
    *body* under *secret*.

    The counterpart to :func:`verify_signature` (#78 Phase 0 Slice 3): the
    live harness uses this to deliver a webhook payload to itself with a
    genuinely valid signature, through the exact same HMAC-SHA256 scheme
    `verify_signature` checks -- never a second, hand-rolled implementation
    that could silently drift from it.
    """
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(body: bytes, headers: Headers, secret: str) -> None:
    """Validate X-Hub-Signature-256 header against HMAC-SHA256 of body.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        body: Raw request body bytes.
        headers: Request headers (Starlette Headers).
        secret: Shared secret configured in Meta app dashboard.

    Raises:
        HTTPException(403): If header is missing, malformed, or digest does not match.
    """
    received_sig = headers.get(_HEADER_NAME)
    if not received_sig:
        raise HTTPException(status_code=403, detail="Missing signature header")

    expected_digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected_sig = f"sha256={expected_digest}"

    if not hmac.compare_digest(expected_sig, received_sig):
        raise HTTPException(status_code=403, detail="Invalid signature")
