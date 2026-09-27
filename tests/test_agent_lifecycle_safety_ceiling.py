"""Phase 4 (#84, live-test plan) -- the one lifecycle assertion that needs no
model: registering a variant of the phase's own custom agent
(`tests/fixtures/agents/phase4_lifecycle/roles/restock-helper`) that tries to
raise a limit above its role hierarchy's ceiling, or loosen
`untrusted_input`, must fail with a clear `DefinitionError` -- through the
exact same `resolve()`/`build_runtime()` path `create_app`'s lifespan calls
for every registered agent at real application boot (`main.py::create_app`'s
own docstring: "one entry that fails to resolve or equip fails the whole
boot").

This mirrors `tests/test_agent_safety_ceiling.py`'s own established pattern
(PR #85's additive-for-capability, subtractive-for-safety rule) applied to
Phase 4's own fixture instead of a synthetic one, and needs neither a live
model nor a reachable Postgres/Redis -- it belongs in the default offline
suite, not behind the `live` marker.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.agent.spec import Agent
from agents_system.harness.factory import build_runtime
from agents_system.harness.loader import DefinitionError, RootConfig, resolve

ROOTS = RootConfig()

_RESTOCK_HELPER_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "agents"
    / "phase4_lifecycle"
    / "roles"
    / "restock-helper"
)

#: `restock-helper` extends `sales-agent`'s own six permissions plus
#: `send:escalation` (inherited from the base `agent` role's own
#: `escalation_notifier`) and additively declares `run:check_stock` --
#: hardcoded here (not imported) so this offline test does not pull in
#: `evals.live_harness`'s live-only dependencies (uvicorn, prometheus_client,
#: ...), the same reasoning `test_agent_safety_ceiling.py`'s own
#: `_SALES_GRANT` already follows.
_RESTOCK_HELPER_GRANT = (
    "read:catalog",
    "read:client_registry",
    "write:orders",
    "write:order_items",
    "read:price_lists",
    "send:message",
    "send:escalation",
    "run:check_stock",
)


def test_restock_helper_resolves_cleanly_as_a_baseline() -> None:
    """Sanity check the fixture itself before probing its ceiling -- a
    failure here means the fixture is broken, not that the ceiling holds."""
    definition = resolve(
        Agent.from_folder(_RESTOCK_HELPER_PATH)._to_locator(), roots=ROOTS
    )
    assert definition.untrusted_input is True
    assert definition.execution_limits is not None
    assert definition.execution_limits["max_tool_calls"] == 5
    assert "run:check_stock" in definition.permissions
    assert any(tool.name == "check_stock" for tool in definition.command_tools)


def test_registering_a_variant_that_raises_the_limit_fails_at_boot() -> None:
    """`restock-helper` tightens `max_tool_calls` to 5; a variant overriding
    it back up past even `sales-agent`'s own inherited ceiling (the platform
    default, 20) must fail -- exactly the check `build_runtime` runs for
    every agent `create_app`'s lifespan resolves at boot."""
    from conftest import build_test_registry

    registry = build_test_registry()
    rogue = Agent.from_folder(
        _RESTOCK_HELPER_PATH, execution_limits={"max_tool_calls": 999}
    )

    with pytest.raises(DefinitionError, match="max_tool_calls") as exc:
        build_runtime(
            rogue._to_locator(),
            registry,
            granted_permissions=_RESTOCK_HELPER_GRANT,
            roots=ROOTS,
        )
    message = str(exc.value)
    assert "max_tool_calls=999" in message
    assert "max_tool_calls=20" in message
    assert "subtractive for safety" in message


def test_registering_a_variant_that_loosens_untrusted_input_fails_at_boot() -> None:
    """`restock-helper` inherits `sales-agent`'s `untrusted_input: true`
    unchanged; a variant declaring `false` must fail -- ADR-002 C.11's
    invariant, checked at the same boot-time `build_runtime` call."""
    from conftest import build_test_registry

    registry = build_test_registry()
    rogue = Agent.from_folder(_RESTOCK_HELPER_PATH, untrusted_input=False)

    with pytest.raises(DefinitionError, match="untrusted_input") as exc:
        build_runtime(
            rogue._to_locator(),
            registry,
            granted_permissions=_RESTOCK_HELPER_GRANT,
            roots=ROOTS,
        )
    assert "restock-helper" in str(exc.value)
