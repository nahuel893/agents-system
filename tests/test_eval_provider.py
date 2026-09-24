"""Offline tests for live-eval model provider selection (#169 follow-up,
ADR-002 E.18).

No network call anywhere here: `_build_chat_model` only reads configuration
at construction time for every provider branch it dispatches to (see
`tests/test_main.py::test_build_chat_model_dispatches_by_provider`, which
this file mirrors for the eval-specific wrapper).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_ollama import ChatOllama

from agentsys.agent.reasoning import ReasoningSanitizedChatOpenAI
from agentsys.config import Settings
from agentsys.evals.provider import build_eval_model, model_display_name


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _patched_settings(settings: Settings) -> tuple[Any, Any]:
    """`build_eval_model` reads `get_settings()` itself, and internally
    calls `main._build_chat_model`, which reads its OWN `get_settings()`
    reference -- a separate name bound at import time in `agentsys.main`.
    Both must be patched to the same settings object for a test to control
    the whole call."""
    return (
        patch("agentsys.evals.provider.get_settings", return_value=settings),
        patch("agentsys.main.get_settings", return_value=settings),
    )


def test_build_eval_model_defaults_to_ollama_with_the_configured_model() -> None:
    settings = _settings(ollama_model="qwen2.5:3b")
    provider_patch, main_patch = _patched_settings(settings)

    with provider_patch, main_patch:
        model, name = build_eval_model()

    assert isinstance(model, ChatOllama)
    assert name == "qwen2.5:3b"


def test_build_eval_model_is_independent_of_adapter_provider() -> None:
    """Changing adapter_provider must never change what the eval uses --
    build_eval_model reads eval_provider, a separate switch."""
    settings = _settings(adapter_provider="anthropic", eval_provider="ollama")
    provider_patch, main_patch = _patched_settings(settings)

    with provider_patch, main_patch:
        model, _name = build_eval_model()

    assert isinstance(model, ChatOllama)


def test_build_eval_model_openai_compatible_reads_the_shared_settings() -> None:
    """eval_provider="openai_compatible" goes through the SAME
    openai_compatible_* settings every other role already uses -- an
    OpenRouter deployment is exactly this (docs/platform/live-eval.md)."""
    settings = _settings(
        eval_provider="openai_compatible",
        openai_compatible_base_url="https://openrouter.ai/api/v1",
        openai_compatible_model="deepseek/deepseek-v4-flash",
        openai_compatible_api_key="test-key",
    )
    provider_patch, main_patch = _patched_settings(settings)

    with provider_patch, main_patch:
        model, name = build_eval_model()

    assert isinstance(model, ReasoningSanitizedChatOpenAI)
    assert model.openai_api_base == "https://openrouter.ai/api/v1"
    assert name == "deepseek/deepseek-v4-flash"


def test_model_display_name_prefers_model_name_over_model() -> None:
    """ChatOpenAI-family classes (groq, openai_compatible) expose the
    configured model as `model_name`, not `model`."""
    fake = SimpleNamespace(model_name="from-model-name", model="from-model")

    assert model_display_name(fake) == "from-model-name"  # type: ignore[arg-type]


def test_model_display_name_falls_back_to_model() -> None:
    """ChatOllama/ChatAnthropic expose the configured model as `model`."""
    fake = SimpleNamespace(model="qwen2.5:3b")

    assert model_display_name(fake) == "qwen2.5:3b"  # type: ignore[arg-type]


def test_model_display_name_falls_back_to_the_class_name() -> None:
    class _NoModelAttrs:
        pass

    assert model_display_name(_NoModelAttrs()) == "_NoModelAttrs"  # type: ignore[arg-type]


def test_model_display_name_ignores_an_empty_model_name() -> None:
    """An empty string is not a usable identifier -- fall through to the
    next attribute rather than reporting a blank name."""
    fake = SimpleNamespace(model_name="", model="qwen2.5:3b")

    assert model_display_name(fake) == "qwen2.5:3b"  # type: ignore[arg-type]
