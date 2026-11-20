"""Channel validation and resolvers for the served app."""

from __future__ import annotations

import re
from collections.abc import (
    Mapping,
)
from typing import Any

from agents_system.extras import raise_missing_extra

try:
    from fastapi import FastAPI
except ImportError as exc:
    raise_missing_extra("server", "The served app", exc)

from agents_system.channels.port import (
    Channel,
    ChannelAdapter,
    ChannelResolver,
)
from agents_system.channels.whatsapp import (
    WHATSAPP_CHANNEL,
)
from agents_system.config import Settings
from agents_system.execution_limits import (
    check_turn_fits_lease,
)
from agents_system.harness.loader import (
    DefinitionError,
)
from agents_system.services.outbox import (
    DEFAULT_LEASE_DURATION,
)
from agents_system.serving.registrations import _bounded_repr

#: A registered channel's name (#22): the ``/webhook/{name}`` path segment
#: and the value stored in ``webhook_inbox.channel``, so deliberately
#: narrower than a runtime id -- lowercase, URL- and log-safe, short.
_CHANNEL_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def _validate_channels(channels: Mapping[str, Channel] | None) -> dict[str, Channel]:
    """``create_app(channels=...)``, validated when the app is created.

    The generic route serves these as soon as the app exists (no lifespan
    needed), so a bad name must fail here rather than at boot.
    ``whatsapp`` is the built-in channel and cannot be re-registered.
    """
    validated: dict[str, Channel] = {}
    for name, channel in (channels or {}).items():
        if not isinstance(name, str) or not _CHANNEL_NAME.fullmatch(name):
            raise DefinitionError(
                f"channels key {_bounded_repr(str(name))} is not a valid channel "
                "name: 1-32 characters of lowercase letters, digits, '_' or "
                "'-', starting with a letter."
            )
        if name == WHATSAPP_CHANNEL:
            raise DefinitionError(
                f"channels[{name!r}] is reserved for the built-in WhatsApp "
                "channel (configured through WHATSAPP_* / META_WEBHOOK_SECRET)."
            )
        if not isinstance(channel, Channel):
            raise DefinitionError(
                f"channels[{name!r}] must be a Channel(adapter=..., "
                f"runtime_id=...), got {type(channel).__name__}."
            )
        if not isinstance(channel.adapter, ChannelAdapter):
            raise DefinitionError(
                f"channels[{name!r}].adapter does not implement ChannelAdapter "
                "(verify_inbound, read_inbound, extract_turn, send_text)."
            )
        if not isinstance(channel.runtime_id, str) or not channel.runtime_id:
            raise DefinitionError(
                f"channels[{name!r}].runtime_id must name a registered runtime "
                "id (a non-empty str)."
            )
        validated[name] = channel
    return validated


def _fixed_resolver(channel: Channel) -> ChannelResolver:
    """A registered channel's adapter, whatever the request's Settings."""

    def resolve(_settings: Settings) -> ChannelAdapter:
        return channel.adapter

    return resolve


def _registered_channels(app: FastAPI) -> Mapping[str, Channel]:
    """The channels ``create_app(channels=...)`` registered (#22)."""
    channels: Mapping[str, Channel] | None = getattr(app.state, "channels", None)
    return channels if channels is not None else {}


def _check_channel_runtime(
    channel_name: str,
    runtime_id: str,
    subject: str,
    definition: Any,
    deployment_limits: Mapping[str, Any] | None = None,
) -> None:
    """The boot-time checks every channel-bound runtime gets.

    ADR-002 C.13: a webhook channel delivers untrusted external input, so the
    runtime answering it must resolve ``untrusted_input=True``. And its turn
    must finish, with ``LEASE_MARGIN_S`` (60 seconds) of non-turn headroom,
    before the deferred worker's outbox lease expires, counting the
    deployment's limits (#278). Applied to the built-in WhatsApp channel
    (``WHATSAPP_RUNTIME_ID``) exactly as before #22, and to every registered
    channel's runtime.
    """
    from agents_system.agent.limits import _effective_limits

    label = (
        "WhatsApp" if channel_name == WHATSAPP_CHANNEL else f"Channel {channel_name!r}"
    )
    if not definition.untrusted_input:
        raise DefinitionError(
            f"Channel {channel_name!r} is bound to {subject} "
            f"(runtime {runtime_id!r}), which resolves "
            f"untrusted_input=False. {label} delivers "
            "untrusted external input, so it must "
            "declare untrusted_input: true (ADR-002 C.11/C.13) "
            "to be bound to it. Refusing to boot."
        )

    effective_limits = _effective_limits(definition.execution_limits, deployment_limits)
    check_turn_fits_lease(
        effective_limits["total_execution_timeout_s"],
        who=f"{label} runtime {runtime_id!r}",
        lease_s=DEFAULT_LEASE_DURATION.total_seconds(),
    )
