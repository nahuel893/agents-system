"""Chat model construction for the served app."""

from __future__ import annotations

from typing import Any

from agents_system.agent import models as _models
from agents_system.config import get_settings
from agents_system.observability.metrics import (
    Metrics,
)


def _build_chat_model(
    provider: str,
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    metrics: Metrics | None = None,
) -> Any:
    """The served app's chat model for ``provider``.

    The implementation is `agent.models.build_chat_model`, shared with the
    library path (issue #186); this wrapper only points it at the server
    ``Settings`` (`get_settings`), which the served app has already validated.
    Kept under this name because the lifespan, the live evals and the test
    suite all reach the served builder through it.
    """
    return _models.build_chat_model(
        provider,
        model_override=model_override,
        temperature=temperature,
        max_tokens=max_tokens,
        metrics=metrics,
        settings_source=get_settings,
    )
