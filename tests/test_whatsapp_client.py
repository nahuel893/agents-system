"""Tests for the outbound WhatsApp client (D-014 slice S1, design AD-2).

No real network calls: httpx.MockTransport backs the AsyncClient in every test.
"""
from __future__ import annotations

import httpx
import pytest

from agentsys.integration.whatsapp_client import WhatsAppClient

BASE_URL = "https://graph.facebook.com/v21.0"
PHONE_NUMBER_ID = "1234567890"
TOKEN = "test-whatsapp-token"


@pytest.mark.asyncio
async def test_send_text_builds_correct_request() -> None:
    """send_text POSTs to {base_url}/{phone_number_id}/messages with Bearer auth."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"messages": [{"id": "wamid.abc"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as async_client:
        client = WhatsAppClient(
            async_client,
            phone_number_id=PHONE_NUMBER_ID,
            token=TOKEN,
            base_url=BASE_URL,
        )
        await client.send_text(to="+5491123456789", body="Hola!")

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/{PHONE_NUMBER_ID}/messages"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = httpx.Request("POST", request.url, content=request.content).content
    import json as _json

    payload = _json.loads(body)
    assert payload["to"] == "+5491123456789"
    assert payload["text"]["body"] == "Hola!"
    assert payload["messaging_product"] == "whatsapp"


@pytest.mark.asyncio
async def test_send_text_raises_on_non_2xx() -> None:
    """A non-2xx Graph API response raises — the caller decides how to handle it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as async_client:
        client = WhatsAppClient(
            async_client,
            phone_number_id=PHONE_NUMBER_ID,
            token=TOKEN,
            base_url=BASE_URL,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_text(to="+5491123456789", body="Hola!")
