"""An image a client sends through the OpenAI-compatible adapter reaches the model.

The request goes through ``POST /v1/chat/completions`` into a REAL
``AgentRuntime``; a fake chat model records exactly what ``_call_model`` hands
it, so the assertions are on the model's input, not on a mocked runtime.

File name note: it sorts after ``test_agent_runtime.py`` on purpose. Any test
that calls ``create_app`` switches logging to JSON, which breaks two text-log
tests there if it runs first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest
from conftest import create_test_app
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from agents_system.agent.graph import AgentRuntime
from agents_system.config import get_settings
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition

MODEL_ID = "acme__vision-agent"
IMAGE_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAfbLI3wAAAABJRU5ErkJggg=="


class _RecordingModel(GenericFakeChatModel):
    """Fake chat model that records the messages of every call."""

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _RecordingModel:
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        self.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client(
    monkeypatch: pytest.MonkeyPatch, *, untrusted_input: bool = False
) -> tuple[TestClient, _RecordingModel]:
    definition = AgentDefinition(
        role_name="vision-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
        untrusted_input=untrusted_input,
    )
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )
    model = _RecordingModel(messages=iter([AIMessage(content="a pixel")]))
    runtime = AgentRuntime(equipped, model)

    app = create_test_app()
    app.state.runtimes = {MODEL_ID: runtime}
    app.state.adapter_model_ids = frozenset({MODEL_ID})
    app.state.engine = MagicMock()

    import agents_system.integration.openai_adapter as adapter_mod

    settings = MagicMock()
    settings.adapter_api_key = ""
    settings.adapter_max_body_bytes = 1_048_576
    monkeypatch.setattr(adapter_mod, "get_settings", lambda: settings)
    return TestClient(app), model


def _last_human_content(model: _RecordingModel) -> Any:
    assert model.seen, "the model was never called"
    humans = [m for m in model.seen[0] if m.type == "human"]
    assert humans, "the model received no human message"
    return humans[-1].content


def test_inbound_image_part_reaches_the_model_as_an_image_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = _client(monkeypatch)
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                ],
            }
        ],
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    content = _last_human_content(model)
    assert isinstance(content, list), f"content was stringified: {content!r}"
    images = [
        p for p in content if isinstance(p, dict) and p.get("type") == "image_url"
    ]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == IMAGE_URL


def test_plain_string_content_still_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = _client(monkeypatch)
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": "hello there"}],
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    assert "hello there" in str(_last_human_content(model))


def test_inbound_image_part_survives_an_untrusted_input_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = _client(monkeypatch, untrusted_input=True)
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                ],
            }
        ],
    }

    response = client.post("/v1/chat/completions", json=payload)

    assert response.status_code == 200
    content = _last_human_content(model)
    assert isinstance(content, list), f"content was stringified: {content!r}"
    images = [
        p for p in content if isinstance(p, dict) and p.get("type") == "image_url"
    ]
    assert len(images) == 1
    assert images[0]["image_url"]["url"] == IMAGE_URL
