"""Integration tests for the openai_compatible chat provider.

These make REAL network calls to whatever ``OPENAI_COMPATIBLE_BASE_URL``
points at. Skipped by default — opt-in with::

    uv run pytest -m integration tests/test_openai_compatible_integration.py

Verified against MiniMax (``https://api.minimax.io/v1``, ``MiniMax-M2.7``).
MiniMax returns its chain-of-thought inline in ``message.content`` and offers
no way to switch that off from the request side, so the "no <think>" assertion
here is the real regression guard for ``ReasoningSanitizedChatOpenAI`` — unit
tests can only prove the stripping logic, not that the provider still behaves
the way we found it.

Two failure modes worth recognising before debugging config:
  - HTTP 429 "Token Plan usage limit reached" — MiniMax enforces a rolling
    quota and recovers in roughly 20 minutes. Retry, do not change settings.
  - HTTP 401 — almost always the WRONG HOST rather than a bad key. The same
    credential that works on api.minimax.io is rejected by api.minimaxi.com
    and api.minimax.chat.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentsys.config import Settings, get_settings
from agentsys.main import _build_chat_model

_CATALOG_SEARCH_TOOL: dict[str, Any] = {
    "name": "catalog_search",
    "description": "Search the product catalog for an item by name.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def _configured_settings() -> Settings:
    """Skip cleanly when the provider is not configured in this environment."""
    get_settings.cache_clear()
    settings = get_settings()
    if not (
        settings.openai_compatible_base_url and settings.openai_compatible_model
    ):
        pytest.skip(
            "OPENAI_COMPATIBLE_BASE_URL / OPENAI_COMPATIBLE_MODEL not configured"
        )
    return settings


@pytest.mark.integration
async def test_live_round_trip_returns_content_without_reasoning() -> None:
    _configured_settings()
    model = _build_chat_model("openai_compatible")

    response = await model.ainvoke("Reply with exactly one word: PONG")

    content = str(response.content)
    assert content.strip(), "model returned empty content"
    assert "<think>" not in content
    assert "</think>" not in content


@pytest.mark.integration
async def test_live_tool_call_survives_sanitization() -> None:
    """The agent binds tools (graph.py:374), so tool-calling must work here.

    MiniMax puts the think block in ``content`` while populating
    ``tool_calls``; this asserts the sanitizer removes the former without
    disturbing the latter.
    """
    _configured_settings()
    model = _build_chat_model("openai_compatible").bind_tools([_CATALOG_SEARCH_TOOL])

    response = await model.ainvoke(
        "Do we have Coca Cola 2L in stock? Use the catalog_search tool."
    )

    assert response.tool_calls, "provider did not emit a tool call"
    assert response.tool_calls[0]["name"] == "catalog_search"
    assert "query" in response.tool_calls[0]["args"]
    assert "<think>" not in str(response.content)
