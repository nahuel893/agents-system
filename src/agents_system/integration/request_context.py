"""Per-request context for the OpenAI-compatible adapter (issue #249).

A channel turn can carry deterministic context (the channel, today's date,
output-format rules) because the application builds the turn itself. A
``/v1/chat/completions`` turn cannot: the adapter builds it. This module is
the extension point that closes the gap. An application passes an async hook
to ``create_app(request_context_hook=...)``; the adapter calls it once per
request with platform metadata only, and attaches the text it returns to that
turn as trusted context (``AgentRuntime.run_turn(trusted_context=...)``).

The hook's output is trusted: the application builds it, so it sits in the
system message, after the agent's own prompt, which keeps priority. User
content stays untrusted, and a client ``system`` message is still dropped.

This module has no dependency on the web stack, so a hook can be written and
tested without the ``server`` extra.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agents_system.subject import TurnSubject

DEFAULT_ADAPTER_CHANNEL = "openai-adapter"


@dataclass(frozen=True)
class AdapterRequestInfo:
    """What the adapter knows about one request, and nothing client-chosen.

    ``runtime_id`` is the registered id the request named as its model.
    ``channel`` is the label the application configured for the adapter
    (``create_app(adapter_channel=...)``). ``request_id`` is the id the
    request-id middleware assigned (the ``x-request-id`` response header), or
    ``None`` when the app runs without it. ``session_id`` is the id the turn
    runs under. ``subject`` is the end user a deployment ``SubjectResolver``
    resolved from the authenticated request, or ``None``.
    """

    runtime_id: str
    channel: str
    request_id: str | None
    session_id: str
    subject: TurnSubject | None


RequestContextHook = Callable[[AdapterRequestInfo], Awaitable[str | None]]
"""Builds the trusted context for one adapter request.

Returns the text to attach to the turn, or ``None`` (or blank text) for none.
Raising, or returning anything other than text or ``None``, fails the request
with a 500 and runs no turn.
"""
