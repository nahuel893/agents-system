"""Issue #186 -- the in-process ``AgentRuntime`` honors the model an agent declares.

An agent declares its own model (``Agent(model={...})`` or ``model:`` in its
frontmatter, inherited through ``extends``, D-037 / #13). The served app already
built a dedicated model from it; the library path used to run whatever model the
caller happened to pass, and the declaration was silently inert.

Both paths now share ONE implementation, ``agents_system.agent.models``:

* ``build_agent_model(definition)`` -- the public helper;
* ``AgentRuntime(equipped)`` -- builds the declared model when the caller passes
  none;
* a caller-passed model AND a different declared one is REPORTED (a structured
  ``runtime.model_conflict`` warning) and the caller's model wins.

Provider credentials are read through a narrow loader (``ProviderSettings``) so
none of this needs the server environment (#167: the server ``Settings`` fail
closed without ``META_WEBHOOK_SECRET``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

import agents_system
from agents_system.config import Settings, get_settings

DECLARED = {
    "provider": "openai_compatible",
    "model": "vendor/declared-model",
    "temperature": 0.25,
    "max_tokens": 321,
}


class ScriptedModel(FakeMessagesListChatModel):
    """A scripted chat model whose ``bind_tools`` does not raise."""

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ScriptedModel:
        return self


class NamedScriptedModel(ScriptedModel):
    """Reports a provider model id, the way a real chat model does."""

    model_name: str = "caller/passed-model"


def _scripted(reply: str = "ok") -> ScriptedModel:
    return ScriptedModel(responses=[AIMessage(content=reply)])


def _clear_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No server variable at all, not even the suite's own ALLOW_INSECURE, so a
    server ``Settings()`` fails closed exactly as it does for an importer."""
    server_fields = set(Settings.model_fields)
    for name in list(os.environ):
        if name.lower() in server_fields:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def library_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The environment of a kit user: provider variables only, and a record of
    every server ``Settings`` construction (there must be none)."""
    _clear_server_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "key-from-environment")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="meta_webhook_secret"):
        Settings()

    constructions: list[str] = []
    real_init = Settings.__init__

    def spying_init(self: Settings, *args: Any, **kwargs: Any) -> None:
        constructions.append("Settings()")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Settings, "__init__", spying_init)
    try:
        yield constructions
    finally:
        get_settings.cache_clear()
        from agents_system.agent.models import get_provider_settings

        get_provider_settings.cache_clear()


def _registry() -> agents_system.ToolRegistry:
    return agents_system.ToolRegistry()


def _equipped(**agent_kwargs: Any) -> Any:
    agent = agents_system.Agent(name="declaring-agent", **agent_kwargs)
    return agents_system.build_runtime(agent, _registry(), granted_permissions=[])


# ---------------------------------------------------------------------------
# The narrow credential loader
# ---------------------------------------------------------------------------


def test_provider_settings_load_without_the_server_environment(
    library_env: list[str],
) -> None:
    from agents_system.agent.models import ProviderSettings

    provider_settings = ProviderSettings()

    assert provider_settings.openai_compatible_base_url == "https://provider.test/v1"
    assert provider_settings.openai_compatible_api_key == "key-from-environment"
    assert library_env == []


def test_server_settings_share_the_provider_fields() -> None:
    """One definition of the provider fields, so the served app and the library
    read the same variables with the same defaults."""
    from agents_system.agent.models import ProviderSettings

    assert issubclass(Settings, ProviderSettings)
    for name in ProviderSettings.model_fields:
        assert Settings.model_fields[name].default == (
            ProviderSettings.model_fields[name].default
        )


# ---------------------------------------------------------------------------
# The public helper
# ---------------------------------------------------------------------------


def test_build_agent_model_builds_the_declared_model_from_the_environment(
    library_env: list[str],
) -> None:
    definition = _equipped(model=DECLARED).definition

    model = agents_system.build_agent_model(definition)

    assert type(model).__name__ == "ReasoningSanitizedChatOpenAI"
    assert model.model_name == "vendor/declared-model"
    assert model.temperature == 0.25
    assert model.max_tokens == 321
    assert model.openai_api_base == "https://provider.test/v1"
    assert model.openai_api_key.get_secret_value() == "key-from-environment"
    assert library_env == [], "the helper constructed the server Settings"


def test_build_agent_model_reads_a_model_inherited_through_extends(
    library_env: list[str],
) -> None:
    parent = agents_system.Agent(name="parent-agent", model=DECLARED)
    child = agents_system.Agent(name="child-agent", extends=parent)
    definition = agents_system.build_runtime(
        child, _registry(), granted_permissions=[]
    ).definition

    model = agents_system.build_agent_model(definition)

    assert model.model_name == "vendor/declared-model"


def test_build_agent_model_returns_the_default_when_nothing_is_declared() -> None:
    default = _scripted()
    definition = _equipped().definition

    assert agents_system.build_agent_model(definition, default_model=default) is default


def test_build_agent_model_without_declaration_or_default_says_so() -> None:
    definition = _equipped().definition

    with pytest.raises(ValueError, match="declares no model") as excinfo:
        agents_system.build_agent_model(definition)

    assert "declaring-agent" in str(excinfo.value)


def test_build_agent_model_names_the_agent_when_a_credential_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    definition = _equipped(
        model={"provider": "anthropic", "model": "claude-3-5-haiku-latest"}
    ).definition

    with pytest.raises(ValueError, match="declaring-agent") as excinfo:
        agents_system.build_agent_model(definition)

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AgentRuntime
# ---------------------------------------------------------------------------


def test_runtime_runs_on_the_declared_model_without_the_caller_building_it(
    library_env: list[str],
) -> None:
    equipped = _equipped(model=DECLARED)

    runtime = agents_system.AgentRuntime(equipped)

    assert runtime._model_id == "vendor/declared-model"
    assert library_env == [], "AgentRuntime constructed the server Settings"


def test_runtime_without_any_model_is_refused_with_a_clear_message() -> None:
    equipped = _equipped()

    with pytest.raises(ValueError, match="declares no model") as excinfo:
        agents_system.AgentRuntime(equipped)

    assert "AgentRuntime(equipped, model=" in str(excinfo.value)


def test_a_caller_model_with_no_declaration_is_used_and_nothing_is_reported() -> None:
    model = _scripted()

    with structlog.testing.capture_logs() as logs:
        runtime = agents_system.AgentRuntime(_equipped(), model=model)

    assert runtime._bound_model is model
    assert not [e for e in logs if e["event"] == "runtime.model_conflict"]


def test_a_conflicting_caller_model_wins_and_is_reported(
    library_env: list[str],
) -> None:
    equipped = _equipped(model=DECLARED)
    caller_model = NamedScriptedModel(responses=[AIMessage(content="ok")])

    with structlog.testing.capture_logs() as logs:
        runtime = agents_system.AgentRuntime(equipped, model=caller_model)

    assert runtime._bound_model is caller_model
    assert runtime._model_id == "caller/passed-model"
    conflicts = [e for e in logs if e["event"] == "runtime.model_conflict"]
    assert len(conflicts) == 1, logs
    assert conflicts[0]["log_level"] == "warning"
    assert conflicts[0]["declared_model"] == "vendor/declared-model"
    assert conflicts[0]["passed_model"] == "caller/passed-model"
    assert conflicts[0]["agent"] == "declaring-agent"
    assert library_env == [], "reporting the conflict must not build the declared model"


def test_a_caller_model_that_is_the_declared_one_is_not_a_conflict() -> None:
    equipped = _equipped(model=DECLARED)
    caller_model = NamedScriptedModel(
        responses=[AIMessage(content="ok")], model_name="vendor/declared-model"
    )

    with structlog.testing.capture_logs() as logs:
        agents_system.AgentRuntime(equipped, model=caller_model)

    assert not [e for e in logs if e["event"] == "runtime.model_conflict"]


def _real_models() -> dict[str, Any]:
    """Real provider classes (none makes a network call on construction), all
    reporting the SAME model id, so only the provider tells them apart."""
    from langchain_anthropic import ChatAnthropic
    from langchain_groq import ChatGroq
    from langchain_ollama import ChatOllama
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    return {
        "openai_compatible": ChatOpenAI(
            model="shared-id",
            api_key=SecretStr("dummy"),
            base_url="https://provider.test/v1",
        ),
        "anthropic": ChatAnthropic(  # type: ignore[call-arg]
            model="shared-id", anthropic_api_key="dummy"
        ),
        "groq": ChatGroq(model="shared-id", api_key=SecretStr("dummy")),
        "ollama": ChatOllama(model="shared-id"),
    }


def _conflicts(logs: list[Any]) -> list[Any]:
    return [e for e in logs if e["event"] == "runtime.model_conflict"]


@pytest.mark.parametrize(
    ("declared_provider", "passed_provider"),
    [
        ("anthropic", "openai_compatible"),
        ("openai_compatible", "anthropic"),
        ("groq", "ollama"),
        ("ollama", "groq"),
    ],
)
def test_the_same_model_id_from_another_provider_is_a_conflict(
    declared_provider: str, passed_provider: str
) -> None:
    declared = {"provider": declared_provider, "model": "shared-id"}
    caller_model = _real_models()[passed_provider]

    with structlog.testing.capture_logs() as logs:
        runtime = agents_system.AgentRuntime(
            _equipped(model=declared), model=caller_model
        )

    assert runtime._bound_model is caller_model
    conflicts = _conflicts(logs)
    assert len(conflicts) == 1, logs
    assert conflicts[0]["declared_model"] == "shared-id"
    assert conflicts[0]["passed_model"] == "shared-id"
    assert conflicts[0]["declared_provider"] == declared_provider
    assert conflicts[0]["passed_provider"] == passed_provider


@pytest.mark.parametrize(
    "provider", ["openai_compatible", "anthropic", "groq", "ollama"]
)
def test_the_same_model_id_from_the_same_provider_is_not_a_conflict(
    provider: str,
) -> None:
    declared = {"provider": provider, "model": "shared-id"}

    with structlog.testing.capture_logs() as logs:
        agents_system.AgentRuntime(
            _equipped(model=declared), model=_real_models()[provider]
        )

    assert not _conflicts(logs)
    assert not [e for e in logs if e["event"] == "runtime.model_provider_unverified"]


def test_an_unidentifiable_model_with_the_declared_id_is_reported_unverified() -> None:
    """A model class whose provider cannot be told (a test double, a custom
    wrapper) that shares the declared id is neither called equivalent nor called
    a conflict: the runtime says it could not establish the provider."""
    declared = {"provider": "anthropic", "model": "shared-id"}
    caller_model = NamedScriptedModel(
        responses=[AIMessage(content="ok")], model_name="shared-id"
    )

    with structlog.testing.capture_logs() as logs:
        runtime = agents_system.AgentRuntime(
            _equipped(model=declared), model=caller_model
        )

    assert runtime._bound_model is caller_model
    assert not _conflicts(logs)
    unverified = [e for e in logs if e["event"] == "runtime.model_provider_unverified"]
    assert len(unverified) == 1, logs
    assert unverified[0]["log_level"] == "info"
    assert unverified[0]["declared_provider"] == "anthropic"
    assert unverified[0]["model"] == "shared-id"


async def test_the_declared_model_answers_the_turn(
    library_env: list[str],
) -> None:
    equipped = _equipped(model=DECLARED)
    answering = _scripted("answered by the declared model")

    with patch("agents_system.agent.models.build_chat_model", return_value=answering):
        runtime = agents_system.AgentRuntime(equipped)
    result = await runtime.run_turn([HumanMessage("hi")], session_id="s")

    assert result[-1].content == "answered by the declared model"


# ---------------------------------------------------------------------------
# One implementation for both paths
# ---------------------------------------------------------------------------


def test_the_served_and_the_in_process_paths_build_the_same_model_the_same_way(
    library_env: list[str],
) -> None:
    """Both paths reach ``agent.models.build_chat_model`` with exactly the same
    arguments for the same definition -- there is no second builder to drift."""
    from agents_system.main import _resolve_model_for_definition

    definition = _equipped(model=DECLARED).definition
    built = MagicMock(name="built-model")

    with patch("agents_system.agent.models.build_chat_model", return_value=built) as b:
        served = _resolve_model_for_definition(definition, MagicMock(), MagicMock())
        library = agents_system.build_agent_model(definition)

    assert served is built
    assert library is built
    assert b.call_count == 2
    assert (
        b.call_args_list[0].args == b.call_args_list[1].args == ("openai_compatible",)
    )
    served_kwargs = dict(b.call_args_list[0].kwargs)
    library_kwargs = dict(b.call_args_list[1].kwargs)
    # Only WHERE the settings come from differs: the served wrapper points the
    # shared builder at the server Settings.
    for kwargs in (served_kwargs, library_kwargs):
        kwargs.pop("settings_source", None)
        kwargs.pop("metrics", None)
    assert (
        served_kwargs
        == library_kwargs
        == {
            "model_override": "vendor/declared-model",
            "temperature": 0.25,
            "max_tokens": 321,
        }
    )


def test_the_library_helper_does_not_import_the_served_app() -> None:
    """The library must not pull FastAPI (`main`) in to build a model."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, agents_system\n"
                "agents_system.build_agent_model\n"
                "agents_system.AgentRuntime\n"
                "assert 'agents_system.main' not in sys.modules\n"
                "assert 'fastapi' not in sys.modules\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
