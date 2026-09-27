"""Tests for issue #145 -- provider-side 429/retry observability.

`MetricsHTTPTransport` wraps the httpx transport a chat model's async client
actually sends requests through, one layer below `AgentRuntime`'s own
turn-level `agent_turns_total{outcome="error"}` counter: it sees every raw
HTTP attempt the provider SDK makes, including the internal retries the SDK
issues (and hides) before a turn ever sees a final success or failure. Every
test drives it against a FAKE inner transport -- no network, no real
provider -- so the recorded metrics are asserted deterministically.
"""

from __future__ import annotations

import httpx
import pytest
from prometheus_client import CollectorRegistry

from agents_system.observability.metrics import build_metrics
from agents_system.observability.provider_http import (
    MetricsHTTPTransport,
    classify_provider_status,
)


class _FakeTransport(httpx.AsyncBaseTransport):
    """A canned inner transport: returns *responses* in order, or raises
    *raises* on the call at *raises*'s index, never touching the network."""

    def __init__(
        self,
        responses: list[httpx.Response] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raises = raises
        self.seen_requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen_requests.append(request)
        if self._raises is not None:
            raise self._raises
        response = self._responses.pop(0)
        response.request = request
        return response


def _request(retry_count: str | None = None) -> httpx.Request:
    headers = {}
    if retry_count is not None:
        headers["x-stainless-retry-count"] = retry_count
    return httpx.Request(
        "POST", "https://example.test/v1/chat/completions", headers=headers
    )


# ---------------------------------------------------------------------------
# classify_provider_status -- pure function, the sole place a raw status
# code is turned into a bounded label value.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "success"),
        (201, "success"),
        (299, "success"),
        (429, "rate_limited"),
        (400, "client_error"),
        (404, "client_error"),
        (499, "client_error"),
        (500, "server_error"),
        (503, "server_error"),
        (599, "server_error"),
    ],
)
def test_classify_provider_status_buckets_known_ranges(
    status_code: int, expected: str
) -> None:
    assert classify_provider_status(status_code) == expected


# ---------------------------------------------------------------------------
# MetricsHTTPTransport -- one raw HTTP attempt, one recorded sample
# ---------------------------------------------------------------------------


async def test_successful_response_is_recorded_as_success() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(200)])
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    response = await transport.handle_async_request(_request(retry_count="0"))

    assert response.status_code == 200
    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="success"
        )._value.get()
        == 1
    )


async def test_429_response_is_recorded_as_rate_limited() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(429)])
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    response = await transport.handle_async_request(_request(retry_count="0"))

    assert response.status_code == 429
    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="rate_limited"
        )._value.get()
        == 1
    )
    # A 429 is not itself a retry attempt -- it is the FIRST attempt that got
    # rate limited. The retry counter fires on the NEXT attempt instead (see
    # below), never on the failing one.
    assert (
        metrics.provider_retries_total.labels(provider="openai_compatible")._value.get()
        == 0
    )


async def test_server_error_and_client_error_are_recorded_distinctly() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(500), httpx.Response(400)])
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    await transport.handle_async_request(_request(retry_count="0"))
    await transport.handle_async_request(_request(retry_count="0"))

    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="server_error"
        )._value.get()
        == 1
    )
    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="client_error"
        )._value.get()
        == 1
    )


async def test_retried_request_increments_retry_counter() -> None:
    """The openai/groq/anthropic SDKs (all Stainless-generated) stamp every
    retried attempt with `x-stainless-retry-count: <n>` for n >= 1 -- the
    first, non-retried attempt carries `0` (or no header at all). This is
    the SDK's own retry loop, invisible to the turn-level outcome, since a
    retry that eventually succeeds never raises."""
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(429), httpx.Response(200)])
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    await transport.handle_async_request(_request(retry_count="0"))
    await transport.handle_async_request(_request(retry_count="1"))

    assert (
        metrics.provider_retries_total.labels(provider="openai_compatible")._value.get()
        == 1
    )


async def test_missing_retry_header_is_never_counted_as_a_retry() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(200)])
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    await transport.handle_async_request(_request(retry_count=None))

    assert (
        metrics.provider_retries_total.labels(provider="openai_compatible")._value.get()
        == 0
    )


async def test_transport_failure_is_recorded_as_network_error_and_reraised() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(raises=httpx.ConnectTimeout("boom"))
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    with pytest.raises(httpx.ConnectTimeout):
        await transport.handle_async_request(_request(retry_count="0"))

    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="network_error"
        )._value.get()
        == 1
    )


async def test_provider_label_distinguishes_providers_on_a_shared_metrics_set() -> None:
    metrics = build_metrics(CollectorRegistry())
    inner = _FakeTransport(responses=[httpx.Response(200)])
    transport = MetricsHTTPTransport(inner, provider="groq", metrics=metrics)

    await transport.handle_async_request(_request(retry_count="0"))

    assert (
        metrics.provider_requests_total.labels(
            provider="groq", status="success"
        )._value.get()
        == 1
    )
    assert (
        metrics.provider_requests_total.labels(
            provider="openai_compatible", status="success"
        )._value.get()
        == 0
    )


async def test_aclose_closes_the_inner_transport() -> None:
    class _TrackingTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__(responses=[])
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    inner = _TrackingTransport()
    metrics = build_metrics(CollectorRegistry())
    transport = MetricsHTTPTransport(
        inner, provider="openai_compatible", metrics=metrics
    )

    await transport.aclose()

    assert inner.closed is True
