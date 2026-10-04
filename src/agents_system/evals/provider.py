"""Live-eval model provider selection (#169 follow-up, ADR-002 E.18).

The eval's own provider choice is deliberately independent of
`Settings.adapter_provider`: flipping which model a manual eval run uses
must never change what the running application actually serves.
`Settings.eval_provider` (env var `EVAL_PROVIDER`, no default) is a
separate switch, accepting the exact same values `main._build_chat_model`
dispatches on -- `"ollama"`, `"groq"`, `"anthropic"`, `"openai_compatible"`.
The `openai_compatible` branch reads the SAME `OPENAI_COMPATIBLE_BASE_URL` /
`OPENAI_COMPATIBLE_MODEL` / `OPENAI_COMPATIBLE_API_KEY` settings every other
role already uses -- see `docs/platform/live-eval.md` for an OpenRouter
worked example.

There is deliberately no default provider (#182): a run that forgot
`EVAL_PROVIDER` used to measure a local model without saying so, so
`build_eval_model` now raises `EvalProviderNotConfiguredError` instead.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from agents_system.agent.graph import model_display_name
from agents_system.config import get_settings
from agents_system.main import _build_chat_model

#: Re-exported for backward compatibility -- `model_display_name` moved to
#: `agent/graph.py` (issue #78 review finding 4) so `AgentRuntime` itself can
#: derive its own default `Settings.model_prices` key from exactly the same
#: logic the live-eval pipeline already used, instead of two copies drifting
#: apart. Every existing `from agents_system.evals.provider import
#: model_display_name` (this module, `evals/__init__.py`, the test suite)
#: keeps working unmodified.
__all__ = [
    "EvalProviderNotConfiguredError",
    "build_eval_model",
    "model_display_name",
]

_PROVIDERS = ("ollama", "groq", "anthropic", "openai_compatible")


class EvalProviderNotConfiguredError(RuntimeError):
    """`EVAL_PROVIDER` is unset, and there is no provider to fall back to."""


def build_eval_model(
    model_override: str | None = None,
) -> tuple[BaseChatModel, str]:
    """Build the chat model a live-eval run should use, from
    `Settings.eval_provider`, plus the display name to report results under.

    Reuses `main._build_chat_model` -- the exact provider dispatch every
    real role is built with -- so an eval run exercises a genuinely
    supported provider, never a bespoke construction path of its own.

    *model_override* names another model of the same provider (the capability
    matrix, #228, builds one per candidate); without it the provider's
    configured model is used.

    Raises `EvalProviderNotConfiguredError` when `EVAL_PROVIDER` is unset:
    picking a model silently would report a measurement of something the
    caller never chose.
    """
    provider = get_settings().eval_provider
    if provider is None:
        raise EvalProviderNotConfiguredError(
            "EVAL_PROVIDER is not set, and there is no default provider: a live "
            "eval or the demo entrypoint would otherwise pick a model without "
            f"telling you. Set EVAL_PROVIDER in your .env or the environment to "
            f"one of {', '.join(_PROVIDERS)} (openai_compatible for a remote "
            "model such as OpenRouter; see docs/platform/live-eval.md) "
            "together with that provider's variables."
        )
    model = _build_chat_model(provider, model_override=model_override)
    return model, model_display_name(model)
