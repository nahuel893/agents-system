"""Shared bounded-body reader for HTTP routes (#140, #37).

Any route that reads a request body before -- or independently of --
authentication must bound that read, or an unauthenticated caller can force
an unbounded-memory read before anything rejects the request. ``POST
/webhook`` (#140, ``integration/webhook.py``) and
``POST /v1/chat/completions`` (#37, ``integration/openai_adapter.py`` --
reachable fully unauthenticated whenever ``adapter_api_key`` is unset) share
this exact problem, so they share this one enforcement primitive instead of
each carrying its own copy.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


async def read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the raw request body while enforcing a byte ceiling.

    Rejects with 413 as soon as the ceiling is crossed -- before any
    downstream parsing, authentication-independent handling, or
    persistence -- so an oversized request costs neither the CPU of
    processing the whole body nor the memory of holding it. A
    client-supplied Content-Length is checked first as a fast path that can
    reject without reading anything, but the ceiling is enforced against
    actual bytes read from the stream regardless: a missing, malformed, or
    understated Content-Length cannot bypass it.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")

    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)
