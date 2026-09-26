"""Tests for `create_app`'s new `agents`/`grants`/`clients` params and
`_validate_runtime_id` (design.md D5/D6, PR4a-i-T1).

Strict TDD: written BEFORE `create_app` gains these parameters — intentionally
red until `main.py` is widened.

Signature/validation only. What `lifespan()` builds from these params
(PR4a-ii) is covered by `tests/test_main.py`'s "registration" tests; the
PR4a-i test that pinned "`agents` is not consumed yet" went with that slice.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents_system.agent.spec import Agent
from agents_system.config import get_settings
from agents_system.harness.loader import DefinitionError
from agents_system.harness.registry import ToolRegistry
from agents_system.main import _validate_runtime_id, create_app


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
