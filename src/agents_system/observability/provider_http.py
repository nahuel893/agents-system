"""Provider-side HTTP observability for chat model calls (issue #145).

Investigation summary (see `docs/platform/observability.md` and the issue
itself for the full evidence): under concurrent real-model load, most turns
that eventually fail do so FAST (a few seconds, well under any of the
platform's own timeouts) with a genuine provider error -- consistent with
the account being rate limited once a real system prompt and bound tools are
in the request. Neither the provider's 429 nor the chat model SDK's own
internal retries (both openai, groq and anthropic's Python SDKs are
Stainless-generated and retry transparently before ever raising) were
visible anywhere: `agent_turns_total{outcome="error"}` only fires once a
turn ultimately fails, after the SDK's retries already ran and hid it, and
carries no information about WHY.

`MetricsHTTPTransport` sits one layer below that: it wraps the httpx
transport a chat model's async client actually sends requests through, so
every raw HTTP attempt -- including ones the SDK's own retry loop makes and
resolves before the caller ever sees an error -- is recorded on
`/metrics` (`agent_provider_requests_total`, `agent_provider_retries_total`;
see `observability/metrics.py`).
"""

from __future__ import annotations

import httpx

from agents_system.observability.metrics import (
    Metrics,
    record_provider_request,
    record_provider_retry,
)

#: Every OpenAI-, Groq- and Anthropic-compatible Python SDK is
#: Stainless-generated and stamps this header on every outgoing request with
#: how many retries THIS logical call has already made -- "0" (or the header
#: simply absent) on the first, non-retried attempt, ">= 1" on every attempt
#: the SDK's own retry loop re-sends after an earlier one failed. This is
#: what makes a transparently-retried call visible at all: it never raises,
#: so nothing else on this call path would ever know it happened.
_STAINLESS_RETRY_HEADER = "x-stainless-retry-count"


def classify_provider_status(status_code: int) -> str:
    """Bucket a raw provider HTTP status code into one of
    `observability.metrics.PROVIDER_REQUEST_STATUSES` -- the only bounded,
    code-defined values ever reaching the `status` label. The raw status
    code and any provider error body are deliberately never labels
    themselves (unbounded cardinality; a body could carry account details).
    """
    if status_code == 429:
        return "rate_limited"
    if 200 <= status_code < 300:
        return "success"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "server_error"
    # Redirects (3xx) and anything below 200 the provider's chat endpoint has
    # no business returning still need a bucket rather than raising here.
    return "client_error"


class MetricsHTTPTransport(httpx.AsyncBaseTransport):
    """Wraps *inner* to record every raw HTTP attempt on *metrics*.

    Composition over subclassing `httpx.AsyncHTTPTransport` so a test can
    inject a fake inner transport and assert on recorded metrics with no
    network at all; production wraps the real
    `httpx.AsyncHTTPTransport()` default. *provider* is one of
    `observability.metrics.PROVIDER_NAMES` -- a bounded, code-chosen value
    from `main._build_chat_model`'s own dispatch, never anything
    caller-supplied at request time.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport, *, provider: str, metrics: Metrics
    ) -> None:
        self._inner = inner
        self._provider = provider
        self._metrics = metrics

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        retry_count = request.headers.get(_STAINLESS_RETRY_HEADER)
        if retry_count not in (None, "0"):
            record_provider_retry(self._metrics, provider=self._provider)

        try:
            response = await self._inner.handle_async_request(request)
        except httpx.TransportError:
            # A connect/read/write timeout (`httpx.TimeoutException`, a
            # `TransportError` subclass) or any other lower-level transport
            # failure (DNS, TCP reset, TLS) -- there is no status code to
            # classify; "network_error" is itself the signal, then re-raise
            # unchanged so every existing caller (the SDK's own retry loop,
            # our per-call timeout) sees exactly the same exception as before
            # this transport existed.
            record_provider_request(
                self._metrics, provider=self._provider, status="network_error"
            )
            raise

        record_provider_request(
            self._metrics,
            provider=self._provider,
            status=classify_provider_status(response.status_code),
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
