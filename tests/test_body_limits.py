"""Tests for the shared bounded-body reader (#140, #37).

``read_bounded_body`` is the shared primitive both ``POST /webhook`` (#140)
and ``POST /v1/chat/completions`` (#37) use to reject an oversized request
body before parsing it. These tests exercise the primitive directly,
independent of either route, covering: the exact-limit boundary on both its
Content-Length fast path and its stream path, a lying (understated)
Content-Length, a missing Content-Length, and a malformed one. Route-level
wiring (that each endpoint actually calls this with its own configured
ceiling) is covered separately in ``tests/test_webhook.py`` and
``tests/test_openai_adapter.py``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from agents_system.integration.body_limits import read_bounded_body


def _make_streaming_request(headers: dict[str, str], chunks: list[bytes]):
    """A minimal Starlette ``Request`` fed body chunks via raw ASGI messages.

    Used to exercise ``read_bounded_body``'s stream-enforcement branch
    directly, independent of whether a test client normalizes
    Content-Length -- a missing or malformed header must not let an
    oversized body slip past the ceiling.
    """
    from starlette.requests import Request as StarletteRequest

    header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": header_list, "method": "POST"}
    remaining = list(chunks)

    async def receive() -> dict:
        if remaining:
            chunk = remaining.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(remaining)}
        return {"type": "http.request", "body": b"", "more_body": False}

    return StarletteRequest(scope, receive)


async def test_read_bounded_body_within_limit_returns_full_body() -> None:
    """A body under the ceiling is read and reassembled unchanged."""
    request = _make_streaming_request({}, [b"abc", b"def"])
    result = await read_bounded_body(request, max_bytes=100)
    assert result == b"abcdef"


async def test_read_bounded_body_exact_limit_accepted_via_content_length() -> None:
    """A body of exactly ``max_bytes``, declared via Content-Length, is
    accepted -- not rejected by the fast path's off-by-one."""
    body = b"x" * 100
    request = _make_streaming_request({"content-length": "100"}, [body])
    result = await read_bounded_body(request, max_bytes=100)
    assert result == body


async def test_read_bounded_body_exact_limit_accepted_via_stream() -> None:
    """A body of exactly ``max_bytes``, with no Content-Length to fast-path
    on, is still accepted by the stream-counting branch alone."""
    body = b"x" * 100
    request = _make_streaming_request({}, [body])
    result = await read_bounded_body(request, max_bytes=100)
    assert result == body


async def test_read_bounded_body_one_over_limit_rejected_via_content_length() -> None:
    """A declared Content-Length one byte over the ceiling is rejected by
    the fast path, without reading the stream."""
    request = _make_streaming_request({"content-length": "101"}, [b"x" * 101])
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_body(request, max_bytes=100)
    assert exc_info.value.status_code == 413


async def test_read_bounded_body_one_over_limit_rejected_via_stream() -> None:
    """A body one byte over the ceiling is rejected by the stream path even
    when no Content-Length short-circuits first."""
    request = _make_streaming_request({}, [b"x" * 60, b"x" * 41])
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_body(request, max_bytes=100)
    assert exc_info.value.status_code == 413


async def test_read_bounded_body_malformed_content_length_falls_back_to_stream() -> (
    None
):
    """A non-integer Content-Length does not bypass the ceiling.

    The fast-path check can't trust it, so enforcement falls through to the
    streamed byte count, which still catches an oversized body.
    """
    request = _make_streaming_request(
        {"content-length": "not-a-number"}, [b"x" * 60, b"x" * 60]
    )
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_body(request, max_bytes=100)
    assert exc_info.value.status_code == 413


async def test_read_bounded_body_missing_content_length_enforced_by_stream() -> None:
    """No Content-Length header at all is still bounded by the streamed count."""
    request = _make_streaming_request({}, [b"x" * 60, b"x" * 60])
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_body(request, max_bytes=100)
    assert exc_info.value.status_code == 413


async def test_read_bounded_body_lying_content_length_understates_stream_enforced() -> (
    None
):
    """A numerically VALID Content-Length that understates the real body must
    not bypass the ceiling.

    Distinct from the malformed-header case above: this header parses fine
    and is well under ``max_bytes``, so the fast Content-Length path lets it
    through -- but the actual streamed bytes exceed the ceiling, and
    enforcement must catch that against the real byte count, not the
    (lying) declared header.
    """
    request = _make_streaming_request({"content-length": "10"}, [b"x" * 60, b"x" * 60])
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_body(request, max_bytes=100)
    assert exc_info.value.status_code == 413
