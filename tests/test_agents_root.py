"""`AGENT_REGISTRATIONS` accepts ``folder:<name>`` under ``AGENTS_ROOT`` (#226).

ADR-010 open question 4: an env-only deployment serves a reference agent
folder. The root is operator-set; a registration value is not trusted, so
every escape fails closed at boot.
"""

from __future__ import annotations

import pathlib
import shutil
from collections.abc import Sequence
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agents_system.config import Settings, get_settings
from agents_system.harness.loader import DefinitionError
from agents_system.main import _settings_agents, create_app, lifespan

REPO = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "agents"


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def agents_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "agents"
    root.mkdir()
    shutil.copytree(EXAMPLES / "summary-agent", root / "summary-agent")
    return root


class _Scripted(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _Scripted:
        return self


def _settings(root: pathlib.Path | str, value: str, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        agents_root=str(root),
        agent_registrations={"ref": value},
        adapter_runtimes=["ref"],
        deploy_grants={"ref": ("read:conversation_logs", "read:knowledge_base")},
        **extra,
    )


async def _boot(settings: Settings, model: Any = None) -> Any:
    """Run the lifespan; return (app, runtimes) captured inside it."""
    from conftest import build_test_registry

    app = create_app(
        registry_factory=lambda *a, **k: build_test_registry(),
        chat_model=model or MagicMock(),
    )
    engine = MagicMock()
    engine.dispose = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(
            patch("agents_system.main.get_settings", return_value=settings)
        )
        stack.enter_context(patch("agents_system.main.get_engine", return_value=engine))
        stack.enter_context(
            patch("agents_system.main.close_redis_pool", new=AsyncMock())
        )

        @asynccontextmanager
        async def _no_checkpointer(*args: Any, **kwargs: Any) -> Any:
            yield MagicMock()

        stack.enter_context(
            patch(
                "agents_system.main._build_checkpointer_cm",
                side_effect=_no_checkpointer,
            )
        )
        stack.enter_context(
            patch(
                "agents_system.services.embeddings.get_embedding_provider",
                return_value=MagicMock(),
            )
        )
        sink_cls = stack.enter_context(patch("agents_system.audit.sink.AuditSink"))
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        async with lifespan(app):
            return dict(app.state.runtimes)


def test_settings_reads_agents_root_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS_ROOT", "/srv/agents")
    assert Settings(_env_file=None).agents_root == "/srv/agents"  # type: ignore[call-arg]


def test_agents_root_defaults_to_unset() -> None:
    assert Settings(_env_file=None).agents_root == ""  # type: ignore[call-arg]


async def test_folder_registration_serves_a_turn(
    agents_root: pathlib.Path,
) -> None:
    model = _Scripted(responses=[AIMessage(content="from the folder agent")])
    runtimes = await _boot(_settings(agents_root, "folder:summary-agent"), model)

    messages = await runtimes["ref"].run_turn(
        [HumanMessage(content="hi")], session_id="s1"
    )
    assert messages[-1].content == "from the folder agent"


def test_folder_value_resolves_to_an_agent_under_the_root(
    agents_root: pathlib.Path,
) -> None:
    agents, clients = _settings_agents(
        {"ref": "folder:summary-agent"}, str(agents_root)
    )
    assert clients == {}
    assert agents["ref"].name == "summary-agent"  # type: ignore[union-attr]


def test_predefined_role_value_still_works_without_a_root() -> None:
    agents, _ = _settings_agents({"ref": "agent"}, "")
    assert agents == {"ref": "agent"}


@pytest.mark.parametrize(
    "value",
    [
        "folder:/etc",
        "folder:/abs/summary-agent",
        "folder:..",
        "folder:../summary-agent",
        "folder:summary-agent/..",
        "folder:sub/summary-agent",
        "folder:.",
        "folder:",
        "folder:summary-agent@acme",
        "folder:..\\x",
        "folder:%s" % ("a" * 500),
    ],
)
def test_escaping_or_malformed_folder_value_fails_closed(
    agents_root: pathlib.Path, value: str
) -> None:
    with pytest.raises(DefinitionError, match=r"AGENT_REGISTRATIONS\['ref'\]"):
        _settings_agents({"ref": value}, str(agents_root))


def test_symlink_out_of_the_root_fails_closed(
    agents_root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    outside = tmp_path / "outside"
    shutil.copytree(EXAMPLES / "summary-agent", outside)
    (agents_root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DefinitionError, match="outside AGENTS_ROOT"):
        _settings_agents({"ref": "folder:linked"}, str(agents_root))


def test_missing_folder_fails_closed(agents_root: pathlib.Path) -> None:
    with pytest.raises(DefinitionError, match="not a directory"):
        _settings_agents({"ref": "folder:nope"}, str(agents_root))


@pytest.mark.parametrize("root", ["", "relative/root"])
def test_unset_or_relative_root_fails_closed(root: str) -> None:
    with pytest.raises(DefinitionError, match="AGENTS_ROOT"):
        _settings_agents({"ref": "folder:summary-agent"}, root)


async def test_escape_fails_boot_through_the_lifespan(
    agents_root: pathlib.Path,
) -> None:
    with pytest.raises(DefinitionError, match="Refusing to boot"):
        await _boot(_settings(agents_root, "folder:../summary-agent"))


async def test_symlinked_file_inside_the_folder_fails_boot(
    agents_root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("outside")
    policy = agents_root / "summary-agent" / "policy.md"
    policy.unlink()
    policy.symlink_to(secret)
    with pytest.raises(DefinitionError):
        await _boot(_settings(agents_root, "folder:summary-agent"))
