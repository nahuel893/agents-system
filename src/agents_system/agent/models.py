"""Chat models from an agent's declared ``model`` (D-037 / #13, issue #186).

An agent declares its own model: ``Agent(model={"provider": ..., "model": ...,
"temperature"?: ..., "max_tokens"?: ...})``, or ``model:`` in its markdown
frontmatter, inherited through ``extends``. Two callers turn that declaration
into a LangChain chat model, and this module is the ONE implementation both
share:

* the served app -- ``main.py``'s lifespan builds one model per runtime;
* the library -- ``AgentRuntime(equipped)`` with no ``model=``, or
  ``agents_system.build_agent_model(definition)`` directly.

Credentials are never part of a declaration (``harness.loader`` refuses any key
outside ``provider``/``model``/``temperature``/``max_tokens``). They are read
here, from the process environment (``GROQ_API_KEY``, ``ANTHROPIC_API_KEY``)
and from ``ProviderSettings`` (``OPENAI_COMPATIBLE_*``, ``OLLAMA_*``, and a
``.env`` file) -- the narrow loader that does NOT require the server
environment. The server ``Settings`` fail closed without ``META_WEBHOOK_SECRET``
(#167), so reading provider credentials through them would make a declared
model unusable in process.

Deliberately importing nothing from ``agents_system.main``: the library must
not pull in FastAPI to build a model.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from agents_system.config import ProviderSettings
from agents_system.observability.metrics import DEFAULT_METRICS, Metrics
from agents_system.observability.provider_http import MetricsHTTPTransport

__all__ = [
    "ProviderSettings",
    "build_agent_model",
    "build_chat_model",
    "declared_model",
    "get_provider_settings",
    "resolve_model_for_definition",
]

# Sent when no API key is configured. ChatOpenAI rejects an empty key at
# construction time, but keyless OpenAI-compatible endpoints are legitimate,
# so the provider needs *something* to hand the client.
_NO_API_KEY_PLACEHOLDER = "not-required"

#: Where a provider reads its host, model and credentials from. The library's
#: default is the narrow ``ProviderSettings``; the served app passes its own
#: ``get_settings`` (a ``Settings`` IS a ``ProviderSettings``).
SettingsSource = Callable[[], ProviderSettings]


@lru_cache
def get_provider_settings() -> ProviderSettings:
    """Cached provider settings -- the library's counterpart of
    ``config.get_settings`` that needs none of the server environment."""
    return ProviderSettings()


def build_chat_model(
    provider: str,
    *,
    model_override: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    metrics: Metrics | None = None,
    settings_source: SettingsSource | None = None,
) -> Any:
    """Construct the chat model for the given provider.

    Returns a LangChain BaseChatModel instance for the configured provider.
    Return typed as Any to avoid mypy false-positives from lazy imports.

    D-037 (issue #13, ADR-004) -- ``model_override``/``temperature``/
    ``max_tokens`` are an OPTIONAL per-agent override (an
    `AgentDefinition.model` declaration, resolved by
    `resolve_model_for_definition`), never a caller requirement: every
    existing call site that names none keeps building byte-identical to the
    pre-D-037 defaults (`temperature=0` for ``openai_compatible``/``ollama``,
    each provider's own hardcoded default model id). Credentials are
    UNCHANGED by this parameter set -- still read exclusively from
    settings/the process environment, never from an override, which is
    exactly why `harness.loader._validate_model_config` never allows a
    credential-shaped key into one.

    issue #145 -- ``metrics`` is an OPTIONAL override of the ``Metrics`` set
    the ``openai_compatible`` branch's provider-request/retry counters are
    recorded on (`observability/provider_http.py::MetricsHTTPTransport`).
    ``None`` (default) uses the process-wide ``DEFAULT_METRICS`` -- the set
    ``GET /metrics`` serves -- so every existing call site is unaffected; a
    test passes its own throw-away set for isolated assertions, the same
    pattern ``AgentRuntime(..., metrics=...)`` already uses. Every other
    provider branch is unaffected by this parameter for now (see the
    module's #145 investigation notes: the reported timeout/throttling
    evidence is all from the ``openai_compatible`` provider actually used in
    production).

    issue #186 -- ``settings_source`` says where the ``openai_compatible`` and
    ``ollama`` branches read their host and model from. ``None`` (default)
    reads ``ProviderSettings`` (`get_provider_settings`), which needs no server
    environment; the served app passes ``config.get_settings``. It is called
    only by the branches that need it, so ``groq``/``anthropic`` never build
    any settings object.
    """
    source = settings_source if settings_source is not None else get_provider_settings

    if provider == "groq":
        from langchain_groq import ChatGroq

        api_key: str | None = os.getenv("GROQ_API_KEY") or None
        groq_kwargs: dict[str, Any] = {}
        if temperature is not None:
            groq_kwargs["temperature"] = temperature
        if max_tokens is not None:
            groq_kwargs["max_tokens"] = max_tokens
        return ChatGroq(
            model=model_override or "llama-3.3-70b-versatile",
            api_key=api_key,  # type: ignore[arg-type]
            **groq_kwargs,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
        if not anthropic_api_key:
            # Issue #146 review: unlike groq (`ChatGroq` raises `GroqError`
            # at construction on a `None` key) and openai_compatible (the
            # explicit guards below), `ChatAnthropic` validates nothing at
            # construction time -- it silently builds a client that would
            # only fail on its first real call, as an opaque authentication
            # error deep inside a turn instead of a boot failure naming the
            # missing variable. With per-agent models (#141) a role can now
            # select its own provider, so this matters more than it used to.
            raise ValueError(
                "ANTHROPIC_API_KEY is required when ADAPTER_PROVIDER=anthropic"
            )
        anthropic_kwargs: dict[str, Any] = {}
        if temperature is not None:
            anthropic_kwargs["temperature"] = temperature
        if max_tokens is not None:
            anthropic_kwargs["max_tokens"] = max_tokens
        return ChatAnthropic(  # type: ignore[call-arg]
            model=model_override or "claude-3-5-haiku-latest",
            anthropic_api_key=anthropic_api_key,
            **anthropic_kwargs,
        )

    if provider == "openai_compatible":
        from agents_system.agent.reasoning import ReasoningSanitizedChatOpenAI

        # Unlike the branches above, this one reads settings instead of raw
        # os.getenv. The divergence is deliberate — unifying all four providers
        # on Settings belongs to D-016, not here. Please don't "fix" it.
        settings = source()
        if not settings.openai_compatible_base_url:
            raise ValueError(
                "OPENAI_COMPATIBLE_BASE_URL is required when "
                "ADAPTER_PROVIDER=openai_compatible"
            )
        model_id = model_override or settings.openai_compatible_model
        if not model_id:
            raise ValueError(
                "OPENAI_COMPATIBLE_MODEL is required when "
                "ADAPTER_PROVIDER=openai_compatible"
            )

        compatible_api_key = settings.openai_compatible_api_key
        if not compatible_api_key:
            # ChatOpenAI refuses to construct with None or "" even though
            # keyless OpenAI-compatible hosts (vLLM, LM Studio, llama.cpp) are
            # perfectly normal. Warn so a genuinely forgotten key shows up at
            # startup rather than as an unexplained 401 much later.
            structlog.get_logger().warning(
                "openai_compatible.no_api_key",
                base_url=settings.openai_compatible_base_url,
            )
            compatible_api_key = _NO_API_KEY_PLACEHOLDER

        openai_compatible_kwargs: dict[str, Any] = {}
        if max_tokens is not None:
            openai_compatible_kwargs["max_tokens"] = max_tokens

        # issue #145 -- a bounded per-call timeout (previously unset, which
        # left the openai SDK's own 600s default in effect) plus a transport
        # that records every raw HTTP attempt's status and the SDK's own
        # retries on /metrics -- see observability/provider_http.py's module
        # docstring for the investigation this responds to. connect stays at
        # the openai SDK's own default (5s); only read/write/pool -- the
        # generation itself -- are bounded by the configured setting.
        resolved_metrics = metrics if metrics is not None else DEFAULT_METRICS
        http_async_client = httpx.AsyncClient(
            transport=MetricsHTTPTransport(
                httpx.AsyncHTTPTransport(),
                provider="openai_compatible",
                metrics=resolved_metrics,
            )
        )
        return ReasoningSanitizedChatOpenAI(
            model=model_id,
            base_url=settings.openai_compatible_base_url,
            api_key=SecretStr(compatible_api_key),
            temperature=temperature if temperature is not None else 0,
            timeout=httpx.Timeout(
                settings.openai_compatible_request_timeout_s, connect=5.0
            ),
            http_async_client=http_async_client,
            **openai_compatible_kwargs,
        )

    # Default: ollama. Model + base URL come from settings (#169, ADR-002
    # E.18) instead of being hardcoded, so a role can be pointed at a
    # different local model (e.g. the live-eval pipeline) without a code
    # change. An empty ollama_base_url becomes None so ChatOllama falls back
    # to its own default host resolution rather than treating "" as a URL.
    from langchain_ollama import ChatOllama

    ollama_settings = source()
    ollama_kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        # ChatOllama has no `max_tokens` field -- its equivalent is
        # `num_predict` (verified against the installed langchain-ollama's
        # own model fields; do not rename this to `max_tokens`).
        ollama_kwargs["num_predict"] = max_tokens
    return ChatOllama(
        model=model_override or ollama_settings.ollama_model,
        base_url=ollama_settings.ollama_base_url or None,
        temperature=temperature if temperature is not None else 0,
        **ollama_kwargs,
    )


def declared_model(definition: Any) -> Mapping[str, Any] | None:
    """The ``model`` declaration ``definition`` resolved to, or ``None``.

    ``definition.model`` is ``None`` for the overwhelming majority of resolved
    definitions -- no role/deployment/`Agent` in the chain declared one. When
    it IS a mapping it was already validated by
    `harness.loader._validate_model_config` at resolution time; this function
    does not re-validate.

    ``isinstance(selection, Mapping)`` (not a truthiness check) deliberately
    rejects anything that is not a real mapping -- including a `MagicMock`
    test double's auto-created `.model` attribute, which is truthy but not a
    mapping, so a test fixture that never explicitly sets `.model` keeps
    resolving to "nothing declared".
    """
    selection = getattr(definition, "model", None)
    if not isinstance(selection, Mapping) or not selection:
        return None
    return selection


def resolve_model_for_definition(
    definition: Any,
    default_model: Any,
    *,
    build: Callable[..., Any] | None = None,
) -> Any:
    """The chat model ``definition``'s own runtime should use (D-037, issue
    #13, ADR-004): a DEDICATED model built from its declaration, else
    ``default_model`` as-is.

    When nothing is declared, ``default_model`` (for the served app, already
    built once from ``settings.adapter_provider``, byte-identical to the
    pre-D-037 single global model) is reused untouched: zero behavior change
    for an existing deployment.

    ``build`` (default: `build_chat_model`, looked up at call time) is the builder called with ``(provider, model_override=...,
    temperature=..., max_tokens=...)``. The library uses `build_chat_model`
    (narrow settings); the served app passes its own wrapper, which points the
    same builder at the server ``Settings``. Everything else -- which
    declaration wins, how it is turned into arguments, how a failure names the
    role -- is here, once.
    """
    selection = declared_model(definition)
    if selection is None:
        return default_model
    builder = build if build is not None else build_chat_model
    try:
        return builder(
            selection["provider"],
            model_override=selection["model"],
            temperature=selection.get("temperature"),
            max_tokens=selection.get("max_tokens"),
        )
    except ValueError as error:
        # Issue #146 review: `build_chat_model` has no runtime/role context of
        # its own -- it builds the SAME shared default model every runtime
        # without its own declaration reuses, so it can only name the missing
        # credential, never which agent needed it. This is the one place that
        # resolves a per-agent `model:` declaration, so it is the only place
        # that can name the role too.
        role_name = getattr(definition, "role_name", "<unknown>")
        raise ValueError(
            f"Role {role_name!r} failed to build its model: {error}"
        ) from error


def build_agent_model(definition: Any, *, default_model: Any = None) -> Any:
    """Build the chat model an agent's resolved ``model`` declaration names.

    The public library helper (``agents_system.build_agent_model``).
    ``definition`` is what ``resolve`` returns, or ``equipped.definition``.
    Credentials come only from the process environment and
    ``ProviderSettings`` (``OPENAI_COMPATIBLE_*``, ``OLLAMA_*``, ``.env``),
    never from the declaration, and never need the server environment.

    An agent that declares no model returns ``default_model`` when one is
    given, and otherwise raises ``ValueError`` -- there is nothing to build,
    and guessing a provider would run the agent on a model nobody chose.
    A declared model that cannot be built (a missing credential, a missing
    host) raises ``ValueError`` naming the agent and the missing variable.
    """
    if default_model is None and declared_model(definition) is None:
        role_name = getattr(definition, "role_name", "<unknown>")
        raise ValueError(
            f"Agent {role_name!r} declares no model, so there is none to build: "
            "give it Agent(model={'provider': ..., 'model': ...}) or a `model:` "
            "block in its policy, or pass a chat model explicitly, e.g. "
            "AgentRuntime(equipped, model=...)."
        )
    return resolve_model_for_definition(definition, default_model)
