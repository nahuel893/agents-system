"""#290 -- the per-runtime outbox retry policy: settings, create_app, boot."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from conftest import create_test_app

from agents_system.config import Settings
from agents_system.main import lifespan
from agents_system.services.retry_policy import (
    DEFAULT_FALLBACK_REPLY,
    OutboxRetryPolicy,
    build_retry_policies,
)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestBuildRetryPolicies:
    def test_nothing_configured_means_no_policies(self) -> None:
        assert build_retry_policies() == {}

    def test_the_environment_values_build_a_policy_per_runtime(self) -> None:
        policies = build_retry_policies(
            max_attempts={"sales": 3},
            fallback_replies={"sales": "Un momento.", "reports": "Listo."},
        )
        assert policies["sales"].max_attempts == 3
        assert policies["sales"].fallback_reply == "Un momento."
        assert policies["reports"].max_attempts == 5
        assert policies["reports"].fallback_reply == "Listo."

    def test_an_explicit_policy_wins_over_the_environment(self) -> None:
        mine = OutboxRetryPolicy(max_attempts=2)
        policies = build_retry_policies(
            policies={"sales": mine},
            max_attempts={"sales": 9},
            fallback_replies={"sales": "ignored"},
        )
        assert policies["sales"] is mine

    def test_a_mapping_is_accepted_for_a_policy(self) -> None:
        policies = build_retry_policies(
            policies={"sales": {"max_attempts": 1, "fallback_reply": "No."}}
        )
        assert policies["sales"].max_attempts == 1
        assert policies["sales"].fallback_reply == "No."
        assert DEFAULT_FALLBACK_REPLY != "No."

    def test_a_bad_value_is_refused_naming_the_runtime(self) -> None:
        with pytest.raises(ValueError, match="'sales'"):
            build_retry_policies(max_attempts={"sales": 0})
        with pytest.raises(ValueError, match="'sales'"):
            build_retry_policies(fallback_replies={"sales": "  "})
        with pytest.raises(ValueError, match="'sales'"):
            build_retry_policies(policies={"sales": 3})  # type: ignore[dict-item]


def test_the_environment_variables_are_validated_at_load() -> None:
    settings = _settings(
        outbox_max_attempts={"sales": 3}, outbox_fallback_reply={"sales": "Un momento."}
    )
    assert settings.outbox_max_attempts == {"sales": 3}
    assert settings.outbox_fallback_reply == {"sales": "Un momento."}
    assert _settings().outbox_max_attempts == {}

    with pytest.raises(ValueError, match="'sales'"):
        _settings(outbox_max_attempts={"sales": 0})
    with pytest.raises(ValueError, match="'sales'"):
        _settings(outbox_fallback_reply={"sales": "  "})


@pytest.mark.asyncio
async def test_boot_refuses_a_policy_for_a_runtime_it_does_not_build() -> None:
    app = create_test_app(
        agents={"reports": "sales-agent"},
        grants={"reports": ["read:catalog"]},
        outbox_retry={"typo": OutboxRetryPolicy(max_attempts=2)},
    )
    settings = _settings(
        adapter_runtimes=["reports"], whatsapp_checkpointer_enabled=False
    )

    with (
        patch("agents_system.main.get_settings", return_value=settings),
        pytest.raises(ValueError, match="typo"),
    ):
        async with lifespan(app):
            pass
