"""The WhatsApp adapter's proactive half (#251): provider errors, classified."""

from __future__ import annotations

import time
from email.utils import formatdate

import httpx
import pytest

from agents_system.channels.port import (
    ProactiveChannel,
    ProviderRateLimited,
    ProviderRejected,
)
from agents_system.channels.whatsapp import WhatsAppChannel
from agents_system.integration.whatsapp_client import WhatsAppClient


def _channel(handler: httpx.MockTransport) -> WhatsAppChannel:
    client = WhatsAppClient(
        httpx.AsyncClient(transport=handler),
        phone_number_id="1",
        token="t",
        base_url="https://graph.test",
    )
    return WhatsAppChannel(sender=client)


def test_the_whatsapp_channel_is_a_proactive_channel() -> None:
    assert isinstance(WhatsAppChannel(), ProactiveChannel)


async def test_a_2xx_send_returns() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    await _channel(httpx.MockTransport(handler)).send_proactive("549111", "hello")
    assert len(seen) == 1


async def test_a_429_carries_the_retry_after_delay() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "42"})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert caught.value.retry_after_s == 42


async def test_a_429_without_a_usable_delay_still_names_one() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": "soon"})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert caught.value.retry_after_s > 0


@pytest.mark.parametrize("raw", ["inf", "nan", "1e12", "-5"])
async def test_a_hostile_retry_after_is_bounded(raw: str) -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": raw})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert 0 < caught.value.retry_after_s <= 86400


async def test_a_4xx_is_a_definitive_rejection() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={}))
    with pytest.raises(ProviderRejected):
        await _channel(transport).send_proactive("549111", "hello")


async def test_a_5xx_is_left_ambiguous() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await _channel(transport).send_proactive("549111", "hello")


async def test_an_http_date_retry_after_is_honored() -> None:
    when = formatdate(time.time() + 7200, usegmt=True)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": when})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert 7100 < caught.value.retry_after_s <= 7200


async def test_an_http_date_in_the_past_falls_back_to_the_default() -> None:
    when = formatdate(time.time() - 7200, usegmt=True)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": when})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert caught.value.retry_after_s == 60


async def test_a_far_future_http_date_is_bounded() -> None:
    when = formatdate(time.time() + 10 * 86400, usegmt=True)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"Retry-After": when})
    )
    with pytest.raises(ProviderRateLimited) as caught:
        await _channel(transport).send_proactive("549111", "hello")
    assert caught.value.retry_after_s == 86400
