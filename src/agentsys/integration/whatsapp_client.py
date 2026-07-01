"""Outbound WhatsApp client — Meta Graph API (D-014 slice S1, design AD-2).

Wraps an injected ``httpx.AsyncClient`` so it is testable without real network
calls (tests substitute a mocked transport) and so its lifecycle is owned by
the app's lifespan (built once, stored on ``app.state.whatsapp_client``).
"""
from __future__ import annotations

import httpx


class WhatsAppClient:
    """Thin wrapper around the Meta Graph API's outbound messages endpoint."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        phone_number_id: str,
        token: str,
        base_url: str,
    ) -> None:
        self._client = client
        self._phone_number_id = phone_number_id
        self._token = token
        self._base_url = base_url

    async def send_text(self, to: str, body: str) -> None:
        """Send a text message to ``to`` via the Meta Graph API.

        Raises ``httpx.HTTPStatusError`` on a non-2xx response. Callers that
        must not crash on send failure (e.g. the webhook handler) are
        responsible for catching this and logging it — this client stays
        honest about failures instead of swallowing them silently.
        """
        url = f"{self._base_url}/{self._phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
