"""HMAC-SHA256 signature verification for Meta WhatsApp Cloud API webhooks."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException
from starlette.datastructures import Headers

_HEADER_NAME = "x-hub-signature-256"


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
