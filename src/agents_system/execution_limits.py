"""Deployment-set execution limits and the turn-timeout reply (#278).

The platform defaults (``PLATFORM_DEFAULT_LIMITS``: 60 s per turn, 10 s per
tool call, 20 tool calls) are the ceiling an agent definition is held to. The
deployment, which pays for and operates the service, may set its own ceiling
per runtime: ``build_runtime(execution_limits=...)``,
``AgentRuntime(execution_limits=...)``, ``create_app(execution_limits=...)``
or ``EXECUTION_LIMITS``. A deployment value replaces the platform default as
that runtime's ceiling; the agent's own declaration may still tighten it,
never exceed it. An agent definition alone still cannot raise anything.

Every deployment-set turn is bounded by the webhook outbox lease: the turn and
its reply send must finish before another worker may reclaim the row, or the
user is answered twice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

#: The webhook outbox lease, in seconds. ``services/outbox.py`` builds its
#: ``DEFAULT_LEASE_DURATION`` from it; it lives here, in the core, so library
#: use checks the same bound without the ``postgres`` extra.
OUTBOX_LEASE_S = 600

#: Seconds of the lease reserved for what surrounds a turn: the checkpointer
#: read before it and the save after it, the reply send, and the outbox
#: bookkeeping. A turn must end before ``lease - margin``.
LEASE_MARGIN_S = 60

#: The part of the margin kept for sending the reply: the channel's HTTP
#: client allows 5 s each to connect, write, read and get a pooled
#: connection. The checkpointer's read and save share the rest, so
#: ``CHECKPOINTER_TIMEOUT_S`` may be at most ``max_checkpointer_timeout_s()``.
REPLY_SEND_ALLOWANCE_S = 20

#: The limits a deployment may set per runtime.
DEPLOYMENT_LIMIT_KEYS = (
    "total_execution_timeout_s",
    "tool_call_timeout_s",
    "max_tool_calls",
    "max_tool_errors",
)

#: What a timed-out turn answers when the deployment configures nothing.
DEFAULT_TIMEOUT_REPLY = "This is taking longer than expected. Please try again."
MAX_TIMEOUT_REPLY_CHARS = 1000

#: What a turn answers when it ends at ``max_tool_errors`` (#289).
DEFAULT_TOOL_ERROR_REPLY = (
    "I could not complete this because a tool kept failing. "
    "Please try again or rephrase your request."
)
MAX_TOOL_ERROR_REPLY_CHARS = 1000


def max_turn_timeout_s(lease_s: float = OUTBOX_LEASE_S) -> float:
    """The bound a lease leaves for a turn; the turn must stay below it."""
    return lease_s - LEASE_MARGIN_S


def check_turn_fits_lease(
    total_s: float, *, who: str, lease_s: float = OUTBOX_LEASE_S
) -> None:
    """Raise ``ValueError`` naming *who* unless the turn plus the margin fits."""
    if total_s + LEASE_MARGIN_S >= lease_s:
        raise ValueError(
            f"{who} has total_execution_timeout_s={total_s}; with the "
            f"{LEASE_MARGIN_S}s margin for the reply send and the checkpointer, "
            f"it must be less than {max_turn_timeout_s(lease_s):g}s to fit the "
            f"outbox lease of {lease_s:g}s"
        )


def max_checkpointer_timeout_s() -> float:
    """The longest checkpointer read or save the margin covers, twice over."""
    return (LEASE_MARGIN_S - REPLY_SEND_ALLOWANCE_S) / 2


def check_margin_covers_checkpointer(checkpointer_timeout_s: float) -> None:
    """Raise ``ValueError`` unless the margin holds the thread read, the save
    and the reply send. Past it, a turn near the bound outlasts the lease and
    another worker runs it again, tools included."""
    if 2 * checkpointer_timeout_s + REPLY_SEND_ALLOWANCE_S > LEASE_MARGIN_S:
        raise ValueError(
            f"CHECKPOINTER_TIMEOUT_S={checkpointer_timeout_s:g} does not fit the "
            f"{LEASE_MARGIN_S}s lease margin: a channel turn reads and saves its "
            f"thread (2 x {checkpointer_timeout_s:g}s) and sends its reply "
            f"({REPLY_SEND_ALLOWANCE_S}s), so it may be at most "
            f"{max_checkpointer_timeout_s():g}s"
        )


def check_effective_limits(
    effective: Mapping[str, Any], *, who: str, lease_s: float = OUTBOX_LEASE_S
) -> None:
    """Check the limits a runtime will actually run with: the deployment's
    ceilings combined with the definition's own values. Each input may be
    valid alone and the combination not."""
    total = effective["total_execution_timeout_s"]
    tool = effective["tool_call_timeout_s"]
    if tool > total:
        raise ValueError(
            f"{who} would run with tool_call_timeout_s={tool} above "
            f"total_execution_timeout_s={total} (the deployment's limits and "
            "the agent's own combined); a tool call cannot outlast the turn it "
            "runs in"
        )
    check_turn_fits_lease(total, who=who, lease_s=lease_s)


_WHOLE_NUMBER_KEYS = ("max_tool_calls", "max_tool_errors")


def _checked_value(key: str, value: Any, *, who: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or (isinstance(value, float) and not math.isfinite(value))
        or value <= 0
    ):
        raise ValueError(
            f"execution_limits for {who}: {key} must be a positive, finite "
            f"number, got {value!r}"
        )
    if key in _WHOLE_NUMBER_KEYS and not isinstance(value, int):
        raise ValueError(
            f"execution_limits for {who}: {key} must be a whole number, got {value!r}"
        )
    return value


def validate_execution_limits(
    raw: Mapping[str, Any] | None, *, who: str, lease_s: float = OUTBOX_LEASE_S
) -> dict[str, int | float]:
    """One runtime's deployment-set limits, checked; ``{}`` for ``None``.

    Fails closed with ``ValueError`` naming *who* on an unknown key, a value
    that is not a positive finite number, a ``tool_call_timeout_s`` above a
    ``total_execution_timeout_s`` set beside it, or a turn that does not fit
    the outbox lease. What one input leaves out is decided only once the
    inputs are combined: ``check_effective_limits``.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(  # noqa: TRY004 - settings validators raise ValueError
            f"execution_limits for {who} must be a mapping of "
            f"{list(DEPLOYMENT_LIMIT_KEYS)}, got {type(raw).__name__}"
        )
    unknown = sorted(str(key) for key in set(raw) - set(DEPLOYMENT_LIMIT_KEYS))
    if unknown:
        raise ValueError(
            f"execution_limits for {who} has unknown key(s) {unknown}; a "
            f"deployment sets only {list(DEPLOYMENT_LIMIT_KEYS)}"
        )
    limits = {key: _checked_value(key, value, who=who) for key, value in raw.items()}

    total = limits.get("total_execution_timeout_s")
    tool = limits.get("tool_call_timeout_s")
    if total is not None and tool is not None and tool > total:
        raise ValueError(
            f"{who} has tool_call_timeout_s={tool} above "
            f"total_execution_timeout_s={total}; a tool call cannot outlast "
            "the turn it runs in"
        )
    if total is not None:
        check_turn_fits_lease(total, who=who, lease_s=lease_s)
    return limits


def parse_execution_limits(
    raw: Mapping[str, Any], *, lease_s: float = OUTBOX_LEASE_S
) -> dict[str, dict[str, int | float]]:
    """``{runtime_id: {total_execution_timeout_s, tool_call_timeout_s,
    max_tool_calls}}``, the shape of ``DEPLOY_GRANTS``; each entry checked by
    ``validate_execution_limits``."""
    if not isinstance(raw, Mapping):
        raise ValueError(  # noqa: TRY004 - settings validators raise ValueError
            f"execution limits must map runtime ids to limits, got {type(raw).__name__}"
        )
    return {
        str(runtime_id): validate_execution_limits(
            value, who=f"runtime {runtime_id!r}", lease_s=lease_s
        )
        for runtime_id, value in raw.items()
    }


def validate_timeout_reply(text: Any, *, who: str) -> str:
    """A timeout reply, checked: non-blank plain text, at most
    ``MAX_TIMEOUT_REPLY_CHARS``. It is sent as given, never templated."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            f"timeout reply for {who} must be non-empty plain text, got {text!r}"
        )
    if len(text) > MAX_TIMEOUT_REPLY_CHARS:
        raise ValueError(
            f"timeout reply for {who} has {len(text)} characters; the most is "
            f"{MAX_TIMEOUT_REPLY_CHARS}"
        )
    return text


def parse_timeout_replies(raw: Mapping[str, Any]) -> dict[str, str]:
    """``{runtime_id: reply}``, the shape of ``DEPLOY_GRANTS``."""
    if not isinstance(raw, Mapping):
        raise ValueError(  # noqa: TRY004 - settings validators raise ValueError
            f"timeout replies must map runtime ids to text, got {type(raw).__name__}"
        )
    return {
        str(runtime_id): validate_timeout_reply(text, who=f"runtime {runtime_id!r}")
        for runtime_id, text in raw.items()
    }


def validate_tool_error_reply(text: Any, *, who: str) -> str:
    """A tool-error reply (#289), checked: non-blank plain text, at most
    ``MAX_TOOL_ERROR_REPLY_CHARS``. It is sent as given, never templated."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            f"tool error reply for {who} must be non-empty plain text, got {text!r}"
        )
    if len(text) > MAX_TOOL_ERROR_REPLY_CHARS:
        raise ValueError(
            f"tool error reply for {who} has {len(text)} characters; the most is "
            f"{MAX_TOOL_ERROR_REPLY_CHARS}"
        )
    return text


def parse_tool_error_replies(raw: Mapping[str, Any]) -> dict[str, str]:
    """``{runtime_id: reply}``, the shape of ``DEPLOY_GRANTS``."""
    if not isinstance(raw, Mapping):
        raise ValueError(  # noqa: TRY004 - settings validators raise ValueError
            f"tool error replies must map runtime ids to text, got {type(raw).__name__}"
        )
    return {
        str(runtime_id): validate_tool_error_reply(text, who=f"runtime {runtime_id!r}")
        for runtime_id, text in raw.items()
    }
