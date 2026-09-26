"""Tests for `integration/meta_signature.py` (#78 Phase 0 Slice 3).

`sign_payload` is the counterpart to `verify_signature`: the live harness
needs to construct a Meta-shaped `X-Hub-Signature-256` header for a webhook
delivery it sends itself, using the exact same HMAC-SHA256 scheme the real
route verifies -- not a second, parallel implementation that could drift.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from agents_system.integration.meta_signature import sign_payload, verify_signature


def test_sign_payload_matches_manual_hmac() -> None:
    body = b'{"hello": "world"}'
    secret = "a-test-secret"

    signature = sign_payload(body, secret)

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert signature == expected


def test_sign_payload_round_trips_through_verify_signature() -> None:
    body = b'{"entry": []}'
    secret = "another-secret"

    signature = sign_payload(body, secret)
    headers = Headers({"x-hub-signature-256": signature})

    # Must not raise.
    verify_signature(body, headers, secret)


def test_sign_payload_is_sensitive_to_body_and_secret() -> None:
    secret = "shared-secret"
    sig_a = sign_payload(b"body-a", secret)
    sig_b = sign_payload(b"body-b", secret)
    sig_c = sign_payload(b"body-a", "different-secret")

    assert sig_a != sig_b
    assert sig_a != sig_c


def test_wrong_signature_is_rejected_by_verify_signature() -> None:
    body = b'{"entry": []}'
    signature = sign_payload(body, "right-secret")
    headers = Headers({"x-hub-signature-256": signature})

    with pytest.raises(HTTPException) as exc_info:
        verify_signature(body, headers, "wrong-secret")
    assert exc_info.value.status_code == 403
