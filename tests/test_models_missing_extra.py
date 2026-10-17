"""A provider whose integration extra is not installed fails naming that extra.

Each provider's chat-model integration is an extra of its own (issue #188):
the failure must be a `MissingExtraError` that says which extra to install,
not a bare `ImportError`, and it must come before any credential is read.
"""

from __future__ import annotations

import sys

import pytest

from agents_system.agent.models import build_chat_model
from agents_system.extras import MissingExtraError


@pytest.mark.parametrize(
    ("provider", "module", "extra"),
    [
        ("groq", "langchain_groq", "groq"),
        ("anthropic", "langchain_anthropic", "anthropic"),
        ("ollama", "langchain_ollama", "ollama"),
    ],
)
def test_a_provider_without_its_extra_names_the_extra_to_install(
    monkeypatch: pytest.MonkeyPatch, provider: str, module: str, extra: str
) -> None:
    # A `None` entry makes `import <module>` raise ImportError.
    monkeypatch.setitem(sys.modules, module, None)

    with pytest.raises(MissingExtraError) as raised:
        build_chat_model(provider)

    assert raised.value.extra == extra
    assert f"[{extra}]" in str(raised.value)
