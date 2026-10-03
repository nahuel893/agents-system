"""ADR-010 Migration step 1 (#222) -- resolving a concrete predefined role by
name is deprecated.

The eight concrete roles become reference agents in ``examples/agents/``.
Until the next breaking release removes them from ``platform/roles/``,
naming one -- ``resolve(name)``, ``extends``, ``create_app(agents=...)`` or
``AGENT_REGISTRATIONS`` -- still works and emits a ``DeprecationWarning``
that names the example folder to copy. ``base`` and ``agent`` stay in the
core and never warn, and neither do the reference agents themselves.
"""

from __future__ import annotations

import pathlib
import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents_system.agent.spec import Agent
from agents_system.harness import loader
from agents_system.harness.loader import (
    DefinitionError,
    PredefinedRoleDeprecationWarning,
    resolve,
)

DEPRECATED_ROLES = (
    "accountant-agent",
    "data-agent",
    "developer-agent",
    "operator-agent",
    "orchestrator",
    "sales-agent",
    "summary-agent",
    "support-agent",
)


def _recorded(action: Any) -> list[warnings.WarningMessage]:
    """Every `PredefinedRoleDeprecationWarning` that ``action()`` emitted."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        action()
    return [
        w for w in caught if issubclass(w.category, PredefinedRoleDeprecationWarning)
    ]


def _write_folder(folder: pathlib.Path, extends: str) -> pathlib.Path:
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f"---\nname: {folder.name}\n---\n\nA test agent.\n", encoding="utf-8"
    )
    (folder / "manifest.md").write_text(
        f"---\nrole: {folder.name}\nextends: {extends}\ntools: []\npermissions: []\n---\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {folder.name}\nautonomy: supervised\n---\n", encoding="utf-8"
    )
    return folder


def test_the_warning_is_a_deprecation_warning() -> None:
    assert issubclass(PredefinedRoleDeprecationWarning, DeprecationWarning)


@pytest.mark.parametrize("name", DEPRECATED_ROLES)
def test_resolving_a_concrete_role_by_name_warns_and_names_the_example_folder(
    name: str,
) -> None:
    with pytest.warns(PredefinedRoleDeprecationWarning) as record:
        definition = resolve(name)

    assert definition.role_name == name
    [warning] = [w for w in record if w.category is PredefinedRoleDeprecationWarning]
    message = str(warning.message)
    assert f"'{name}'" in message
    assert f"examples/agents/{name}/" in message
    assert "Agent.from_folder" in message


def test_the_warning_points_at_the_callers_line_not_the_loader() -> None:
    """Attributed to the first frame outside the package, so the adopter sees
    their own line, and Python's default filter shows it from `__main__`."""
    [warning] = _recorded(lambda: resolve("sales-agent"))

    assert pathlib.Path(warning.filename) == pathlib.Path(__file__)


def test_a_role_that_extends_another_concrete_role_warns_once() -> None:
    """`developer-agent` extends `operator-agent`; only the name the caller
    wrote is deprecated, so the walk inside the platform tree adds nothing."""
    [warning] = _recorded(lambda: resolve("developer-agent"))

    assert "examples/agents/developer-agent/" in str(warning.message)
    assert "operator-agent/" not in str(warning.message)


@pytest.mark.parametrize("extends", ["sales-agent", "platform/roles/sales-agent"])
def test_an_inline_agent_extending_a_concrete_role_warns(extends: str) -> None:
    agent = Agent(name="restock-helper", extends=extends)

    with pytest.warns(
        PredefinedRoleDeprecationWarning, match="examples/agents/sales-agent/"
    ):
        resolve(agent)


def test_a_folder_agent_extending_a_concrete_role_warns(tmp_path: pathlib.Path) -> None:
    folder = _write_folder(tmp_path / "agents" / "restock-helper", "data-agent")

    with pytest.warns(
        PredefinedRoleDeprecationWarning, match="examples/agents/data-agent/"
    ):
        resolve(Agent.from_folder(folder))


def test_the_warning_is_also_logged_for_a_served_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python's default filters hide a `DeprecationWarning` raised under a
    server, so the loader logs it too."""
    logger = MagicMock()
    monkeypatch.setattr(loader, "logger", logger)

    _recorded(lambda: resolve("summary-agent"))

    logger.warning.assert_called_once()
    args, kwargs = logger.warning.call_args
    assert args == ("role.deprecated",)
    assert kwargs["role"] == "summary-agent"
    assert kwargs["copy_from"] == "examples/agents/summary-agent/"


# ---------------------------------------------------------------------------
# What never warns
# ---------------------------------------------------------------------------


def test_the_generic_parents_never_warn(tmp_path: pathlib.Path) -> None:
    folder_agent = _write_folder(tmp_path / "agents" / "plain", "agent")
    folder_base = _write_folder(tmp_path / "agents" / "rooted", "platform/roles/base")

    def resolve_every_generic_form() -> None:
        resolve("agent")
        resolve(Agent(name="default-parent"))
        resolve(Agent(name="prefixed-parent", extends="platform/roles/agent"))
        resolve(Agent(name="named-agent", extends="agent"))
        resolve(Agent(name="named-base", extends="base"))
        resolve(Agent.from_folder(folder_agent))
        resolve(Agent.from_folder(folder_base))
        with pytest.raises(DefinitionError, match="abstract"):
            resolve("base")

    assert _recorded(resolve_every_generic_form) == []


def test_the_reference_agents_never_warn() -> None:
    examples = pathlib.Path(__file__).resolve().parent.parent / "examples" / "agents"

    def resolve_every_reference_agent() -> None:
        for name in DEPRECATED_ROLES:
            resolve(Agent.from_folder(examples / name))

    assert _recorded(resolve_every_reference_agent) == []


# ---------------------------------------------------------------------------
# Served by name
# ---------------------------------------------------------------------------


async def test_agent_registrations_naming_a_concrete_role_warns() -> None:
    from conftest import build_test_registry

    from agents_system.config import Settings
    from agents_system.main import create_app, lifespan

    fake_model = MagicMock(name="fake-chat-model")
    fake_model.bind_tools.return_value = fake_model
    application = create_app(
        registry_factory=lambda *a, **k: build_test_registry(), chat_model=fake_model
    )
    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"sales": "sales-agent"},
        adapter_runtimes=["sales"],
        deploy_grants={"sales": ["read:catalog"]},
        whatsapp_checkpointer_enabled=False,
    )

    engine = MagicMock()
    engine.dispose = AsyncMock()
    booted: list[str] = []

    async def boot() -> None:
        async with lifespan(application):
            booted.extend(application.state.runtimes)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=engine),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        with pytest.warns(
            PredefinedRoleDeprecationWarning, match="examples/agents/sales-agent/"
        ):
            await boot()

    assert booted == ["sales"]
