"""Execution limits: which limit keys the runtime enforces, how a deployment's limits tighten the platform defaults, and the names of the terminal graph nodes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS

# Terminal node reached when a turn exhausts its max_tool_calls budget.
_LIMIT_REACHED_NODE = "limit_reached"


# Terminal node reached when a turn's tool errors reach max_tool_errors (#289).
_TOOL_ERRORS_NODE = "tool_errors_reached"


# The subset of PLATFORM_DEFAULT_LIMITS the agent loop enforces (it also
# carries max_delegation_depth/max_clarification_attempts, not yet read here).
_ENFORCED_LIMIT_KEYS = (
    "max_tool_calls",
    "max_tool_errors",
    "total_execution_timeout_s",
    "tool_call_timeout_s",
    "max_context_tokens",
)


def _effective_limits(
    execution_limits: Mapping[str, Any] | None,
    deployment_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a runtime's execution_limits over the platform defaults, per-key.

    Design AD-3: a partially-specified execution_limits dict (e.g. only
    ``max_tool_calls`` overridden) falls back to the platform default for
    every key it does not itself set.

    #278: a key the deployment sets replaces the platform default and caps
    the definition's own value, so the definition may tighten it, never
    exceed it -- including a platform role that declares above the default.
    A tool-call timeout neither of them sets is capped at the turn's, so a
    turn lowered below the 10 s default does not outlast its own tool call.
    A non-finite declared value raises ``ValueError``: it would bound nothing.
    """
    ceiling = deployment_limits or {}
    declared = execution_limits or {}
    merged: dict[str, Any] = {
        key: ceiling.get(key, PLATFORM_DEFAULT_LIMITS[key])
        for key in _ENFORCED_LIMIT_KEYS
    }
    for key in _ENFORCED_LIMIT_KEYS:
        value = declared.get(key)
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(
                f"execution_limits sets {key}={value}, not a finite number"
            )
        merged[key] = min(value, ceiling[key]) if key in ceiling else value
    tool_key = "tool_call_timeout_s"
    if tool_key not in ceiling and declared.get(tool_key) is None:
        merged[tool_key] = min(merged[tool_key], merged["total_execution_timeout_s"])
    return merged


def _tighter(*ceilings: Mapping[str, Any]) -> dict[str, Any]:
    """Per key, the lowest value any of the *ceilings* sets."""
    merged: dict[str, Any] = {}
    for ceiling in ceilings:
        for key, value in ceiling.items():
            merged[key] = min(value, merged[key]) if key in merged else value
    return merged
