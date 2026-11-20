"""Validation and resolution of the ``model`` block a definition may declare."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from agents_system.harness.definition.errors import DefinitionError

#: D-037 (issue #13, ADR-004) -- the provider values `serving.models._build_chat_model`
#: dispatches on. Kept identical to `Settings.adapter_provider`'s `Literal`
#: (config.py); the loader cannot import `main`/`config` here without risking
#: a cycle (`main` imports this module), so
#: `tests/test_model_configuration.py::
#: test_supported_providers_are_exactly_the_adapter_provider_literal` pins
#: the two sets equal instead.
_SUPPORTED_MODEL_PROVIDERS = frozenset(
    {"ollama", "groq", "anthropic", "openai_compatible"}
)


#: The closed key set a `model:` declaration may use. Closed deliberately:
#: `provider`/`model` select WHICH already-configured provider/model to use,
#: never a credential -- `serving.models._build_chat_model` reads every real secret
#: exclusively from `Settings`/the process environment. A key outside this
#: set (an attempted `api_key`, `token`, `secret`, ...) is rejected by
#: `_validate_model_config`, so a versioned manifest structurally cannot
#: carry one.
_MODEL_CONFIG_KEYS = frozenset({"provider", "model", "temperature", "max_tokens"})


#: A generous ceiling most hosted providers document as their own maximum
#: (matches `config._MAX_PRICE_PER_MILLION`'s "reject nonsense, not tune to
#: the provider" spirit) -- exists only to catch a misplaced-decimal or
#: unit-mixup typo at boot, not to model any one provider's exact range.
_MAX_MODEL_TEMPERATURE = 2.0


#: Same "reject nonsense, not tune to the provider" spirit as
#: `_MAX_MODEL_TEMPERATURE` above, for `max_tokens` (PR #141 review): no
#: shipped provider documents an output-token limit anywhere near this, so
#: it exists only to catch a misplaced-decimal or unit-mixup typo
#: (`max_tokens: 1000000000` for `1000`) at boot, never to bound a real one.
_MAX_MODEL_MAX_TOKENS = 1_000_000


def _validate_model_config(value: Mapping[str, Any], *, who: str) -> None:
    """Validate a resolved `model:` declaration (D-037, ADR-004).

    Unlike `autonomy`/`execution_limits`/`audit_policy`, model/provider
    selection is not a capability or safety ceiling -- it never widens what
    a role can do -- so this checks SHAPE and VALUES only. There is no
    parent/deployment ceiling to compare against, and no
    `_apply_importer_ceiling` involvement.

    Raises `DefinitionError` naming ``who``, the field and the reason, the
    same style every other loader invariant uses.
    """
    unknown = sorted(set(value) - _MODEL_CONFIG_KEYS)
    if unknown:
        raise DefinitionError(
            f"Invariant violation — model: {who} declares unknown key(s) "
            f"{unknown} in its model configuration. Allowed keys: "
            f"{sorted(_MODEL_CONFIG_KEYS)}. Credentials are never read from "
            "here -- they come only from Settings/the process environment "
            "(e.g. ANTHROPIC_API_KEY, GROQ_API_KEY)."
        )

    provider = value.get("provider")
    if provider not in _SUPPORTED_MODEL_PROVIDERS:
        raise DefinitionError(
            f"Invariant violation — model: {who} declares "
            f"provider={provider!r}, which is not one of "
            f"{sorted(_SUPPORTED_MODEL_PROVIDERS)}."
        )

    model_id = value.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise DefinitionError(
            f"Invariant violation — model: {who} declares model={model_id!r}, "
            "which must be a non-empty string naming the provider's model id."
        )

    if "temperature" in value:
        temperature = value["temperature"]
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, int | float)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= _MAX_MODEL_TEMPERATURE
        ):
            raise DefinitionError(
                f"Invariant violation — model: {who} declares "
                f"temperature={temperature!r}, which must be a finite number "
                f"between 0 and {_MAX_MODEL_TEMPERATURE}."
            )

    if "max_tokens" in value:
        max_tokens = value["max_tokens"]
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise DefinitionError(
                f"Invariant violation — model: {who} declares "
                f"max_tokens={max_tokens!r}, which must be a positive integer."
            )
        if max_tokens > _MAX_MODEL_MAX_TOKENS:
            raise DefinitionError(
                f"Invariant violation — model: {who} declares "
                f"max_tokens={max_tokens!r}, which exceeds the sanity ceiling "
                f"of {_MAX_MODEL_MAX_TOKENS} (catches a misplaced-decimal or "
                "unit-mixup typo, not any one provider's real limit)."
            )


def _resolve_model_declaration(
    value: Mapping[str, Any] | str | None, *, who: str
) -> Mapping[str, Any] | None:
    """Resolve one already-folded `model:` declaration to either `None`
    ("not declared here") or a validated dict (PR #141 review).

    `None` and the literal string `"inherit"` both mean "not declared" --
    the vocabulary `_merge_validated` already used for a deployment
    override that says nothing, reused here for EVERY site that reads a
    `model:` value so a malformed one (a bare scalar, a list, a typo'd
    string -- anything that is not a mapping) can no longer slip past as
    one of those two meanings. Before this helper existed, a non-dict value
    other than `None` was silently coerced to `None` at three separate call
    sites (a role's own declaration on the no-deployment-override path in
    `resolve()`, a deployment override's own declaration in
    `_merge_validated`, and -- because `_merge_validated`'s "override says
    nothing" branch took `generic.model` straight from the role without
    ever validating it -- a role's malformed declaration read through ANY
    deployment override that itself did not touch `model:`, silently
    passing every `_validate_model_config` invariant (allowlisted
    providers, a required model id, the closed credential-shaped key set,
    numeric temperature/max_tokens bounds) for that common case.
    """
    if value is None or value == "inherit":
        return None
    if isinstance(value, dict):
        _validate_model_config(value, who=who)
        return dict(value)
    raise DefinitionError(
        f"Invariant violation — model: {who} declares a malformed model: "
        f"value {value!r}; expected a mapping with 'provider'/'model' keys "
        "(or omit the key, or set it to 'inherit', to inherit)."
    )
