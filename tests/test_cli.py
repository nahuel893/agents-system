"""Issue #312 -- the package's command line: `agents check`, `chat`, `serve`, `db`.

Every subcommand runs through `agents_system.cli.main(argv)`, the function both
console scripts (`agents` and `agents-system`) point at, with its output
captured. Nothing here reaches a model, a server or a database: `chat` runs on a
scripted model, `serve` and `db` on recorded calls.
"""

from __future__ import annotations

import pathlib
import sys
import textwrap
import tomllib
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agents_system import cli
from agents_system.cli import main

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures: a folder agent and a registry module the CLI imports by name
# ---------------------------------------------------------------------------
def _write_agent(
    base: pathlib.Path,
    name: str,
    *,
    tools: list[str],
    permissions: list[str],
    extends: str = "agent",
) -> pathlib.Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nBody.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nextends: {extends}\n'
        f"tools: [{', '.join(tools)}]\nskills: []\ncontext: {{}}\n"
        f"permissions: [{', '.join(permissions)}]\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits:\n  max_tool_calls: 5\n"
        "memory_policy:\n  read_scope: session\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return folder


_REGISTRY_MODULE = textwrap.dedent(
    """
    import agents_system


    def word_count(inputs, /):
        return {"words": len(inputs["text"].split())}


    def catalog_search(inputs, /):
        return {"items": ["Item Alpha"]}


    def _build():
        registry = agents_system.ToolRegistry()
        registry.register(
            agents_system.ToolSpec(
                name="word_count",
                description="Count the words in a text.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
                required_permissions=(),
                connector=word_count,
                tier=agents_system.Tier.T0,
            )
        )
        registry.register(
            agents_system.ToolSpec(
                name="catalog_search",
                description="Search the catalog.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                required_permissions=("read:catalog",),
                connector=catalog_search,
                tier=agents_system.Tier.T1,
            )
        )
        return registry


    registry = _build()


    def build_registry():
        return _build()


    def served_factory(settings, embedder, bi_engine):
        return _build()


    not_a_registry = 42
    """
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`main` configures structlog for the process, bound to the stderr of
    the moment, which is this test's capture; the next module gets back the
    configuration it had."""
    import structlog

    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


@pytest.fixture
def workdir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A project directory holding `cli_tools.py`, as the CLI's cwd."""
    (tmp_path / "cli_tools.py").write_text(_REGISTRY_MODULE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    sys.modules.pop("cli_tools", None)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# The command itself
# ---------------------------------------------------------------------------
def test_both_console_scripts_point_at_the_same_function() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]

    assert scripts == {
        "agents": "agents_system.cli:main",
        "agents-system": "agents_system.cli:main",
    }


def test_help_lists_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for subcommand in ("check", "chat", "serve", "db"):
        assert subcommand in out


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    import agents_system

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert agents_system.__version__ in capsys.readouterr().out


def test_no_subcommand_prints_usage_and_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = _run([], capsys)

    assert code == 2
    assert "usage:" in err


def test_an_unknown_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["deploy"])

    assert exit_info.value.code == 2


def test_importing_the_cli_needs_no_server_or_database_module() -> None:
    import subprocess

    probe = (
        "import sys\n"
        "import agents_system.cli\n"
        "loaded = sorted(m for m in ('fastapi', 'uvicorn', 'sqlalchemy', 'alembic')"
        " if m in sys.modules)\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# agents check
# ---------------------------------------------------------------------------
def test_check_reports_an_equipped_tool(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(
        workdir, "shop", tools=["catalog_search"], permissions=["read:catalog"]
    )

    code, out, _ = _run(
        [
            "check",
            "./shop",
            "--registry",
            "cli_tools:registry",
            "--grant",
            "read:catalog",
        ],
        capsys,
    )

    assert code == 0, out
    assert "catalog_search" in out
    line = next(line for line in out.splitlines() if "catalog_search" in line)
    assert "equipped" in line
    assert "denied" not in line


def test_check_says_a_permission_the_agent_does_not_declare(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #310's case: the caller grants it, the agent's ceiling does not
    # declare it.
    _write_agent(workdir, "shop", tools=["catalog_search"], permissions=[])

    code, out, _ = _run(
        [
            "check",
            "./shop",
            "--registry",
            "cli_tools:registry",
            "--grant",
            "read:catalog",
        ],
        capsys,
    )

    assert code == 0, out
    line = next(line for line in out.splitlines() if "catalog_search" in line)
    assert "denied" in line
    assert "read:catalog is not in the agent's permissions" in out


def test_check_says_a_permission_the_caller_does_not_grant(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(
        workdir, "shop", tools=["catalog_search"], permissions=["read:catalog"]
    )

    code, out, _ = _run(["check", "./shop", "--registry", "cli_tools:registry"], capsys)

    assert code == 0, out
    line = next(line for line in out.splitlines() if "catalog_search" in line)
    assert "denied" in line
    assert "read:catalog is not granted by the caller" in out


def test_check_says_a_tool_the_registry_lacks(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(workdir, "shop", tools=["inventory_sync"], permissions=[])

    code, out, _ = _run(["check", "./shop", "--registry", "cli_tools:registry"], capsys)

    assert code == 0, out
    line = next(line for line in out.splitlines() if "inventory_sync" in line)
    assert "denied" in line
    assert "not in the registry" in out


def test_check_prints_the_chain_permissions_limits_and_memory_policy(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(workdir, "shop", tools=["word_count"], permissions=["read:catalog"])

    code, out, _ = _run(["check", "./shop", "--registry", "cli_tools:registry"], capsys)

    assert code == 0, out
    assert "shop -> agent -> base" in out
    assert "read:catalog" in out
    assert "max_tool_calls" in out and "5" in out
    assert "read_scope" in out and "session" in out


def test_check_fills_a_base_tool_with_its_built_in_default(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The base role's escalation_notifier, absent from the user's registry,
    # is the library's built-in default (issue #168): equipped, and said so.
    _write_agent(workdir, "shop", tools=[], permissions=[])

    code, out, _ = _run(
        [
            "check",
            "./shop",
            "--registry",
            "cli_tools:registry",
            "--grant",
            "send:escalation",
        ],
        capsys,
    )

    assert code == 0, out
    line = next(line for line in out.splitlines() if "escalation_notifier" in line)
    assert "equipped" in line
    assert "built-in default" in line


def test_check_without_a_registry_uses_the_reference_tools(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(
        workdir,
        "helper",
        tools=["knowledge_retrieval", "word_count"],
        permissions=["read:knowledge_base"],
    )

    code, out, _ = _run(["check", "./helper", "--grant", "read:knowledge_base"], capsys)

    assert code == 0, out
    knowledge = next(line for line in out.splitlines() if "knowledge_retrieval" in line)
    assert "equipped" in knowledge
    own = next(line for line in out.splitlines() if "word_count" in line)
    assert "not in the registry" in own


def test_check_accepts_a_registry_factory(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(workdir, "shop", tools=["word_count"], permissions=[])

    for target in ("cli_tools:build_registry", "cli_tools:served_factory"):
        code, out, _ = _run(["check", "./shop", "--registry", target], capsys)
        assert code == 0, (target, out)
        line = next(line for line in out.splitlines() if "word_count" in line)
        assert "equipped" in line, target


def test_check_resolves_a_predefined_role_by_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(["check", "agent"], capsys)

    assert code == 0, out
    assert "agent -> base" in out
    assert "escalation_notifier" in out


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("./no-such-folder", "no-such-folder"),
        ("no-such-role", "no-such-role"),
    ],
)
def test_check_fails_when_the_agent_does_not_resolve(
    workdir: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
    expected: str,
) -> None:
    code, _, err = _run(["check", target], capsys)

    assert code == 1
    assert expected in err
    assert "Traceback" not in err


def test_check_fails_on_a_permission_name_nothing_registers(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(workdir, "shop", tools=[], permissions=[])

    code, _, err = _run(["check", "./shop", "--grant", "read:nothing_here"], capsys)

    assert code == 1
    assert "read:nothing_here" in err


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("cli_tools", "module:attribute"),
        ("cli_tools:missing", "missing"),
        ("cli_tools:not_a_registry", "ToolRegistry"),
        ("no_such_module_here:registry", "no_such_module_here"),
    ],
)
def test_check_fails_on_a_bad_registry(
    workdir: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
    expected: str,
) -> None:
    _write_agent(workdir, "shop", tools=[], permissions=[])

    code, _, err = _run(["check", "./shop", "--registry", target], capsys)

    assert code == 1
    assert expected in err


def test_check_keeps_the_runtime_logs_off_stdout(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(
        workdir, "shop", tools=["catalog_search"], permissions=["read:catalog"]
    )

    _, out, _ = _run(["check", "./shop", "--registry", "cli_tools:registry"], capsys)

    assert "injector." not in out
    assert "factory.runtime_built" not in out


# ---------------------------------------------------------------------------
# agents chat
# ---------------------------------------------------------------------------
class _ScriptedModel(FakeMessagesListChatModel):
    """Answers from a script, and records every message list it was sent."""

    seen: ClassVar[list[list[BaseMessage]]] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Any:
        type(self).seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """The model `agents chat` builds is this scripted one; yields the build
    calls (provider, model) so a test can check what was asked for."""
    calls: list[Any] = []
    _ScriptedModel.seen = []

    def fake_model(definition: Any, *, provider: str | None, model: str | None) -> Any:
        calls.append((provider, model))
        return _ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "word_count",
                            "args": {"text": "the quick brown fox"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content=[{"type": "text", "text": "There are 4 words."}]),
                AIMessage(content="You asked about 4 words."),
            ]
        )

    monkeypatch.setattr(cli.chat, "build_model", fake_model)
    yield calls


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """The user's input lines; Ctrl-D (EOFError) once they run out."""
    remaining = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def test_chat_shows_the_tool_call_and_prints_the_reply_text(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scripted: list[Any],
) -> None:
    _write_agent(workdir, "counter", tools=["word_count"], permissions=[])
    _feed(monkeypatch, ["How many words in 'the quick brown fox'?", "exit"])

    code, out, _ = _run(
        ["chat", "./counter", "--registry", "cli_tools:registry"], capsys
    )

    assert code == 0, out
    assert '-> word_count({"text": "the quick brown fox"})' in out
    assert '<- word_count: {"words": 4}' in out
    # The text of the reply, never its raw content blocks.
    assert "There are 4 words." in out
    assert "{'type': 'text'" not in out


def test_chat_keeps_the_whole_history_tool_calls_included(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scripted: list[Any],
) -> None:
    _write_agent(workdir, "counter", tools=["word_count"], permissions=[])
    _feed(monkeypatch, ["How many words in 'the quick brown fox'?", "And now?"])

    code, out, _ = _run(
        ["chat", "./counter", "--registry", "cli_tools:registry"], capsys
    )

    assert code == 0, out
    assert "You asked about 4 words." in out
    last_call = _ScriptedModel.seen[-1]
    kinds = [type(message) for message in last_call if message.type != "system"]
    assert kinds == [HumanMessage, AIMessage, ToolMessage, AIMessage, HumanMessage]


def test_chat_ends_on_end_of_input(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scripted: list[Any],
) -> None:
    _write_agent(workdir, "counter", tools=["word_count"], permissions=[])
    _feed(monkeypatch, [])

    code, _, _ = _run(["chat", "./counter", "--registry", "cli_tools:registry"], capsys)

    assert code == 0
    assert _ScriptedModel.seen == []


def test_chat_passes_the_model_and_provider_flags(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scripted: list[Any],
) -> None:
    _write_agent(workdir, "counter", tools=["word_count"], permissions=[])
    _feed(monkeypatch, [])

    _run(
        [
            "chat",
            "./counter",
            "--registry",
            "cli_tools:registry",
            "--provider",
            "anthropic",
            "--model",
            "claude-haiku-5-5",
        ],
        capsys,
    )

    assert scripted == [("anthropic", "claude-haiku-5-5")]


def test_chat_fails_before_the_loop_when_the_agent_does_not_resolve(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scripted: list[Any],
) -> None:
    _feed(monkeypatch, ["hello"])

    code, _, err = _run(["chat", "./missing"], capsys)

    assert code == 1
    assert "missing" in err
    assert scripted == []


# The real model builder, without the scripted stand-in.
class _Definition:
    role_name = "counter"

    def __init__(self, model: dict[str, Any] | None = None) -> None:
        self.model = model


def test_build_model_uses_the_provider_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[Any] = []
    monkeypatch.setattr(
        cli.chat,
        "build_chat_model",
        lambda provider, **kwargs: built.append((provider, kwargs)) or "model",
    )

    assert (
        cli.chat.build_model(_Definition(), provider="groq", model="llama-x") == "model"
    )
    assert built == [("groq", {"model_override": "llama-x"})]


def test_build_model_follows_the_agents_declared_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(
        cli.chat,
        "build_chat_model",
        lambda provider, **kwargs: built.append((provider, kwargs)) or "model",
    )
    declared = {
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "max_tokens": 900,
    }

    cli.chat.build_model(_Definition(declared), provider=None, model=None)
    cli.chat.build_model(_Definition(declared), provider=None, model="claude-haiku-5-5")

    assert built == [
        (
            "anthropic",
            {
                "model_override": "claude-sonnet-5-5",
                "temperature": None,
                "max_tokens": 900,
            },
        ),
        (
            "anthropic",
            {
                "model_override": "claude-haiku-5-5",
                "temperature": None,
                "max_tokens": 900,
            },
        ),
    ]


def test_build_model_falls_back_to_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADAPTER_PROVIDER", "openai_compatible")
    built: list[Any] = []
    monkeypatch.setattr(
        cli.chat,
        "build_chat_model",
        lambda provider, **kwargs: built.append((provider, kwargs)) or "model",
    )

    cli.chat.build_model(_Definition(), provider=None, model=None)

    assert built == [("openai_compatible", {"model_override": None})]


def test_build_model_without_any_provider_says_how_to_choose_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADAPTER_PROVIDER", raising=False)

    with pytest.raises(cli.CLIError, match="--provider"):
        cli.chat.build_model(_Definition(), provider=None, model="some-model")


def test_chat_reports_a_model_that_cannot_be_built(
    workdir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_agent(workdir, "counter", tools=["word_count"], permissions=[])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    code, _, err = _run(
        [
            "chat",
            "./counter",
            "--registry",
            "cli_tools:registry",
            "--provider",
            "anthropic",
        ],
        capsys,
    )

    assert code == 1
    assert "ANTHROPIC_API_KEY" in err


# ---------------------------------------------------------------------------
# agents serve
# ---------------------------------------------------------------------------
@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    import uvicorn

    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs))
    )
    monkeypatch.delenv("AGENTS_ROOT", raising=False)
    monkeypatch.delenv("REGISTRY_FACTORY", raising=False)
    return calls


def test_serve_runs_the_served_app_with_uvicorn(
    uvicorn_calls: list[tuple[Any, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = _run(["serve"], capsys)

    assert code == 0
    assert uvicorn_calls == [
        (
            "agents_system.server:get_application",
            {
                "factory": True,
                "host": "127.0.0.1",
                "port": 8000,
                "workers": None,
                "reload": False,
            },
        )
    ]


def test_serve_passes_its_flags(
    uvicorn_calls: list[tuple[Any, dict[str, Any]]],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os

    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents").mkdir()

    code, _, _ = _run(
        [
            "serve",
            "--agents-root",
            "agents",
            "--registry",
            "my_app.tools:build_registry",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--workers",
            "2",
        ],
        capsys,
    )

    assert code == 0
    _, kwargs = uvicorn_calls[0]
    assert kwargs == {
        "factory": True,
        "host": "0.0.0.0",
        "port": 9000,
        "workers": 2,
        "reload": False,
    }
    # AGENTS_ROOT must be absolute: the served app refuses a relative one.
    assert os.environ["AGENTS_ROOT"] == str(tmp_path / "agents")
    assert os.environ["REGISTRY_FACTORY"] == "my_app.tools:build_registry"


def test_serve_reload(
    uvicorn_calls: list[tuple[Any, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(["serve", "--reload"], capsys)

    assert uvicorn_calls[0][1]["reload"] is True


def test_serve_refuses_an_agents_root_that_is_not_a_directory(
    uvicorn_calls: list[tuple[Any, dict[str, Any]]],
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = _run(["serve", "--agents-root", str(tmp_path / "nope")], capsys)

    assert code == 1
    assert "nope" in err
    assert uvicorn_calls == []


# ---------------------------------------------------------------------------
# agents db
# ---------------------------------------------------------------------------
@pytest.fixture
def alembic_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, Any, tuple[Any, ...]]]:
    from alembic import command

    calls: list[tuple[str, Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(
        command,
        "upgrade",
        lambda config, *args: calls.append(("upgrade", config, args)),
    )
    monkeypatch.setattr(
        command,
        "current",
        lambda config, *args, **kwargs: calls.append(("current", config, args)),
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return calls


def test_db_upgrade_applies_the_packaged_migrations_to_head(
    alembic_calls: list[tuple[str, Any, tuple[Any, ...]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agents_system

    code, _, _ = _run(["db", "upgrade"], capsys)

    assert code == 0
    operation, config, args = alembic_calls[0]
    assert (operation, args) == ("upgrade", ("head",))
    location = pathlib.Path(config.get_main_option("script_location"))
    assert location == pathlib.Path(agents_system.__file__).parent / "migrations"
    assert (location / "env.py").is_file()


def test_db_database_url_overrides_the_configured_one(
    alembic_calls: list[tuple[str, Any, tuple[Any, ...]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os

    url = "postgresql+asyncpg://user:pw@db.example:5432/agents"

    code, _, _ = _run(["db", "upgrade", "--database-url", url], capsys)

    assert code == 0
    # `migrations/env.py` reads DATABASE_URL ahead of the Settings.
    assert os.environ["DATABASE_URL"] == url


def test_db_current_prints_the_applied_revision(
    alembic_calls: list[tuple[str, Any, tuple[Any, ...]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = _run(["db", "current"], capsys)

    assert code == 0
    assert [call[0] for call in alembic_calls] == ["current"]


def test_db_without_an_action_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["db"])

    assert exit_info.value.code == 2


def test_the_packaged_migrations_are_every_revision() -> None:
    import agents_system

    versions = pathlib.Path(agents_system.__file__).parent / "migrations" / "versions"
    revisions = sorted(path.name for path in versions.glob("*.py"))

    assert revisions[0].startswith("001_")
    assert len(revisions) == len({name[:3] for name in revisions})
    # The repository's alembic.ini points at the same directory.
    ini = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    assert "script_location = %(here)s/src/agents_system/migrations" in ini


# ---------------------------------------------------------------------------
# A subcommand whose extra is missing names it
# ---------------------------------------------------------------------------
_BLOCKER = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED = {blocked!r}

    class _NotInstalled(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            for module in BLOCKED:
                if name == module or name.startswith(module + "."):
                    raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
            return None

    sys.meta_path.insert(0, _NotInstalled())

    from agents_system.cli import main

    sys.exit(main({argv!r}))
    """
)


@pytest.mark.parametrize(
    ("argv", "blocked", "extra"),
    [
        (["serve"], ("fastapi", "uvicorn"), "server"),
        (["db", "upgrade"], ("alembic", "sqlalchemy"), "postgres"),
        (["db", "current"], ("alembic", "sqlalchemy"), "postgres"),
    ],
)
def test_a_subcommand_without_its_extra_names_it(
    argv: list[str], blocked: tuple[str, ...], extra: str
) -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", _BLOCKER.format(blocked=blocked, argv=argv)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 1, result.stderr
    assert f"agents-system[{extra}]" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_names_the_registry_it_used(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agent(workdir, "shop", tools=[], permissions=[])

    _, default_out, _ = _run(["check", "./shop"], capsys)
    _, own_out, _ = _run(
        ["check", "./shop", "--registry", "cli_tools:registry"], capsys
    )

    assert "reference tools" in default_out
    assert "cli_tools:registry" in own_out


def test_check_shows_that_a_predefined_role_is_deprecated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ADR-010: naming a concrete predefined role still resolves it, and warns.
    # The CLI's logs are errors only, so the report itself must say so.
    code, out, _ = _run(["check", "summary-agent"], capsys)

    assert code == 0, out
    assert "Warnings:" in out
    assert "deprecated" in out
    assert "examples/agents/summary-agent/" in out


def test_extends_chain_lists_the_leaf_and_its_ancestors(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent
    from agents_system.harness.loader import extends_chain

    folder = _write_agent(tmp_path, "shop", tools=[], permissions=[])
    inline = Agent(name="vip-shop", extends=Agent.from_folder(folder))

    assert extends_chain("agent") == ("agent", "base")
    assert extends_chain(Agent.from_folder(folder)) == ("shop", "agent", "base")
    assert extends_chain(inline) == ("vip-shop", "shop", "agent", "base")


def test_check_reports_a_t3_grant_to_an_untrusted_agent(
    workdir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # build_runtime refuses the grant itself (UntrustedInputGrantError): one
    # line on stderr, never a traceback.
    folder = _write_agent(workdir, "intake", tools=[], permissions=[])
    policy = folder / "policy.md"
    policy.write_text(
        policy.read_text(encoding="utf-8").replace(
            "autonomy: supervised\n", "autonomy: supervised\nuntrusted_input: true\n"
        ),
        encoding="utf-8",
    )

    code, _, err = _run(["check", "./intake", "--grant", "exec:command"], capsys)

    assert code == 1
    assert "exec:command" in err
    assert "Traceback" not in err
