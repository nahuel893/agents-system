"""Characterization tests for the module split (#337).

They pin what the three modules about to be split produce today, so a move
that changes behavior fails here even where no other test looks:

* the resolved definition of every predefined role and reference agent;
* the system prompt each one is equipped with;
* the attributes the served app keeps on ``app.state``.

The expected values live in ``tests/snapshots/module_split.json``. A long value
is stored as its length and hash, so the file stays small and a diff still
names the field that moved. After an intended change, regenerate it with
``UPDATE_SNAPSHOTS=1 pytest tests/test_module_split_characterization.py`` and
review the diff.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import os
import pathlib
import warnings
from collections.abc import Mapping
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import build_test_registry, create_test_app

from agents_system import Agent, build_runtime
from agents_system.config import Settings, get_settings
from agents_system.harness import DefinitionError, resolve
from agents_system.main import lifespan

REPO = pathlib.Path(__file__).resolve().parent.parent
PLATFORM_ROLES = REPO / "platform" / "roles"
REFERENCE_AGENTS = REPO / "examples" / "agents"
SNAPSHOT = pathlib.Path(__file__).resolve().parent / "snapshots" / "module_split.json"

_LONG = 200


def _folders(root: pathlib.Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.is_dir())


PREDEFINED_ROLES = _folders(PLATFORM_ROLES)
REFERENCE_AGENT_NAMES = _folders(REFERENCE_AGENTS)


def _plain(value: Any) -> Any:
    """Turn a resolved value into JSON types, the same on every machine."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=str)}
    if isinstance(value, set | frozenset):
        return sorted((_plain(item) for item in value), key=str)
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, pathlib.Path):
        return value.as_posix().replace(REPO.as_posix(), "<repo>")
    if isinstance(value, enum.Enum):
        return _plain(value.value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def _compact(value: Any) -> Any:
    """Keep a short value as it is; replace a long one by its length and hash."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) <= _LONG:
        return value
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _fingerprint(definition: Any, equipped_prompt: str) -> dict[str, Any]:
    fields = {name: _compact(item) for name, item in _plain(definition).items()}
    fields["equipped_system_prompt"] = _compact(equipped_prompt)
    return fields


def _resolve_and_equip(target: str | Agent) -> dict[str, Any]:
    with warnings.catch_warnings():
        # A concrete predefined role warns that it is deprecated; that warning
        # is not what this test pins.
        warnings.simplefilter("ignore")
        try:
            definition = resolve(target)
        except DefinitionError as error:
            # An abstract role refuses to resolve; the refusal is what is pinned.
            return {"refused": str(error)}
        equipped = build_runtime(target, build_test_registry(), granted_permissions=[])
    return _fingerprint(definition, equipped.system_prompt)


def _check(section: str, key: str, actual: Any) -> None:
    stored = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.is_file() else {}
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        stored.setdefault(section, {})[key] = actual
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        return
    expected = stored.get(section, {}).get(key)
    assert expected is not None, (
        f"no snapshot for {section}/{key}; see the module docstring"
    )
    if isinstance(expected, dict) and isinstance(actual, dict):
        changed = sorted(
            name
            for name in expected.keys() | actual.keys()
            if expected.get(name) != actual.get(name)
        )
        assert not changed, f"{section}/{key} changed in: {changed}"
    assert actual == expected


@pytest.mark.parametrize("role", PREDEFINED_ROLES)
def test_predefined_role_resolves_to_the_same_definition(role: str) -> None:
    _check("predefined_roles", role, _resolve_and_equip(role))


@pytest.mark.parametrize("name", REFERENCE_AGENT_NAMES)
def test_reference_agent_resolves_to_the_same_definition(name: str) -> None:
    agent = Agent.from_folder(REFERENCE_AGENTS / name)
    _check("reference_agents", name, _resolve_and_equip(agent))


def test_snapshot_covers_exactly_the_roles_and_agents_that_exist() -> None:
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        pytest.skip("regenerating")
    stored = json.loads(SNAPSHOT.read_text())
    assert sorted(stored["predefined_roles"]) == PREDEFINED_ROLES
    assert sorted(stored["reference_agents"]) == REFERENCE_AGENT_NAMES


def test_compact_separates_values_that_differ_only_past_the_cut() -> None:
    shared = "x" * (_LONG * 2)
    assert _compact(shared + "a") != _compact(shared + "b")
    assert _compact("short") == "short"


@pytest.fixture
def clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _state_names(app: Any) -> list[str]:
    return sorted(vars(app.state)["_state"])


async def test_app_state_carries_the_same_attributes(
    clear_settings_cache: None, tmp_path: pathlib.Path
) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        agents_root=str(REFERENCE_AGENTS),
        agent_registrations={"ref": "folder:summary-agent"},
        adapter_runtimes=["ref"],
        deploy_grants={"ref": ("read:conversation_logs", "read:knowledge_base")},
    )
    app = create_test_app(
        registry_factory=lambda *args, **kwargs: build_test_registry(),
        chat_model=MagicMock(),
    )
    after_create = _state_names(app)

    engine = MagicMock()
    engine.dispose = AsyncMock()

    @asynccontextmanager
    async def no_checkpointer(*args: Any, **kwargs: Any) -> Any:
        yield MagicMock()

    with ExitStack() as stack:
        stack.enter_context(
            patch("agents_system.main.get_settings", return_value=settings)
        )
        stack.enter_context(patch("agents_system.main.get_engine", return_value=engine))
        stack.enter_context(
            patch("agents_system.main.close_redis_pool", new=AsyncMock())
        )
        stack.enter_context(
            patch(
                "agents_system.main._build_checkpointer_cm",
                side_effect=no_checkpointer,
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
            inside_lifespan = _state_names(app)

    _check("app_state", "after_create_app", after_create)
    _check("app_state", "inside_lifespan", inside_lifespan)
