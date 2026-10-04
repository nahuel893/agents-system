"""Issue #172 -- a role's turn budget must be able to hold provider calls.

`total_execution_timeout_s` (the role's turn budget) bounds the whole turn;
`OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S` bounds one provider HTTP call. A turn
that uses tools makes at least two provider calls, so a budget below the
request timeout lets a single slow call consume the whole turn -- which is
how `developer-agent` (inherited 30s vs a 45s request timeout) ended every
live eval run in `runtime.timeout`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agents_system.config import Settings
from agents_system.harness.loader import resolve
from agents_system.main import _warn_if_turn_budget_below_request_timeout

#: ADR-010 (#222): these tests resolve concrete predefined roles by name, which
#: is deprecated. They move to `agent` or the reference agents in
#: `examples/agents/` when the roles leave `platform/roles/` (#229).
pytestmark = pytest.mark.filterwarnings(
    "ignore::agents_system.harness.loader.PredefinedRoleDeprecationWarning"
)

_REQUEST_TIMEOUT_S = Settings.model_fields[
    "openai_compatible_request_timeout_s"
].default


def test_developer_agent_turn_budget_fits_two_provider_calls() -> None:
    """A tool round trip is two provider calls; the budget must hold both at
    the request timeout, plus the tool's own time, or one slow call is a
    guaranteed timeout."""
    limits = resolve("developer-agent").execution_limits

    assert limits is not None
    assert limits["total_execution_timeout_s"] >= 2 * _REQUEST_TIMEOUT_S


def test_developer_agent_keeps_the_operator_agents_subprocess_ceilings() -> None:
    """Only the turn budget moved: every call is still a subprocess, so the
    per-tool timeout and the tool-call ceiling stay as `operator-agent` set
    them."""
    limits = resolve("developer-agent").execution_limits

    assert limits is not None
    assert limits["tool_call_timeout_s"] == 10
    assert limits["max_tool_calls"] == 10


def _definition(total_s: float | None, model: dict[str, Any] | None = None) -> Any:
    limits = None if total_s is None else {"total_execution_timeout_s": total_s}
    return SimpleNamespace(role_name="some-role", execution_limits=limits, model=model)


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        **overrides,  # type: ignore[arg-type]
    )


def test_boot_warns_when_the_turn_budget_is_below_the_request_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _warn_if_turn_budget_below_request_timeout(
        "dev", _definition(30), _settings(adapter_provider="openai_compatible")
    )

    out = capsys.readouterr().out
    assert "runtime.turn_budget_below_request_timeout" in out
    assert "runtime_id=dev" in out
    assert "total_execution_timeout_s=30" in out


def test_boot_stays_quiet_when_the_turn_budget_covers_the_request_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _warn_if_turn_budget_below_request_timeout(
        "dev", _definition(120), _settings(adapter_provider="openai_compatible")
    )

    assert "turn_budget_below_request_timeout" not in capsys.readouterr().out


def test_boot_uses_the_platform_default_budget_when_the_role_sets_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """60s (platform default) is above the 45s default request timeout."""
    _warn_if_turn_budget_below_request_timeout(
        "dev", _definition(None), _settings(adapter_provider="openai_compatible")
    )

    assert "turn_budget_below_request_timeout" not in capsys.readouterr().out


def test_boot_ignores_providers_the_request_timeout_does_not_apply_to(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The setting only bounds `openai_compatible` calls."""
    _warn_if_turn_budget_below_request_timeout(
        "dev", _definition(30), _settings(adapter_provider="ollama")
    )

    assert "turn_budget_below_request_timeout" not in capsys.readouterr().out


def test_boot_reads_the_provider_from_the_roles_own_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = {"provider": "openai_compatible", "model": "some-model"}
    _warn_if_turn_budget_below_request_timeout(
        "dev", _definition(30, model), _settings(adapter_provider="ollama")
    )

    assert "runtime.turn_budget_below_request_timeout" in capsys.readouterr().out
