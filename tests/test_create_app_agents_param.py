"""Tests for `create_app`'s new `agents`/`grants`/`clients` params and
`_validate_runtime_id` (design.md D5/D6, PR4a-i-T1).

Strict TDD: written BEFORE `create_app` gains these parameters — intentionally
red until `main.py` is widened.

Signature/validation only. `lifespan()` does not read `agents`/`grants`/
`clients` yet (that is PR4a-ii) — this file asserts that explicitly: passing
`agents` has no observable effect on `app.state.runtimes` in this slice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents_system.agent.spec import Agent
from agents_system.config import Settings, get_settings
from agents_system.harness.loader import DefinitionError
from agents_system.harness.registry import ToolRegistry
from agents_system.main import _validate_runtime_id, create_app, lifespan


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# PR4a-i-T1 — `create_app` accepts `agents`/`grants`/`clients`
# ---------------------------------------------------------------------------


def test_create_app_accepts_a_str_valued_agents_entry() -> None:
    """A bare platform-role-name string value is accepted and stashed."""
    app = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        agents={"acme-sales": "sales-agent"},
    )
    assert app.state.agents == {"acme-sales": "sales-agent"}


def test_create_app_accepts_an_agent_valued_agents_entry() -> None:
    """An `Agent` instance value is accepted and stashed unchanged."""
    triage = Agent(name="triage-bot")
    app = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        agents={"triage-bot": triage},
    )
    assert app.state.agents == {"triage-bot": triage}


def test_create_app_accepts_grants_and_clients() -> None:
    """`grants`/`clients` are accepted and stashed the same way `agents` is."""
    app = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        agents={"acme-sales": "sales-agent"},
        grants={"acme-sales": ("read:catalog",)},
        clients={"acme-sales": "acme"},
    )
    assert app.state.grants == {"acme-sales": ("read:catalog",)}
    assert app.state.clients == {"acme-sales": "acme"}


def test_create_app_defaults_the_new_params_to_none() -> None:
    """No caller ever has to pass these — the existing zero-arg-beyond-
    registry_factory call (e.g. `demo.py`'s `build_app`) keeps working."""
    app = create_app(registry_factory=lambda *a, **k: ToolRegistry())
    assert app.state.agents is None
    assert app.state.grants is None
    assert app.state.clients is None


@pytest.mark.asyncio
async def test_agents_param_has_no_effect_on_lifespan_yet() -> None:
    """`lifespan()` still only reads the Settings-driven fallback path in
    this slice — an `agents` mapping is written to `app.state` but not yet
    consumed. Booting with `adapter_runtimes=[]` and a non-empty `agents`
    stays a no-runtimes boot, proving `agents` has no observable effect yet."""
    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        adapter_runtimes=[],
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    app = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        agents={"acme-sales": "sales-agent"},
    )

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
    ):
        async with lifespan(app):
            assert app.state.runtimes == {}


# ---------------------------------------------------------------------------
# PR4a-i-T1 — `_validate_runtime_id`
# ---------------------------------------------------------------------------


def test_validate_runtime_id_rejects_empty_string() -> None:
    with pytest.raises(DefinitionError):
        _validate_runtime_id("")


def test_validate_runtime_id_rejects_a_string_with_spaces() -> None:
    with pytest.raises(DefinitionError):
        _validate_runtime_id("bad id with spaces")


def test_validate_runtime_id_returns_a_valid_id_unchanged() -> None:
    assert _validate_runtime_id("acme-sales-v2") == "acme-sales-v2"
