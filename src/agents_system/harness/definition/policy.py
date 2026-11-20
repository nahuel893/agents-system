"""Policy constants: the autonomy ranks, the platform default limits, the policy field names and the rule sentences an error message quotes."""

from __future__ import annotations

from agents_system.agent.context import DEFAULT_MAX_CONTEXT_TOKENS

# ---------------------------------------------------------------------------
# Autonomy rank — lower rank is more restrictive (safer)
# ---------------------------------------------------------------------------
_AUTONOMY_RANK: dict[str, int] = {
    "confirm": 0,
    "supervised": 1,
    "full": 2,
}


#: What a chain resolves to when no definition in it declares an autonomy
#: level -- the platform floor ``load_generic`` applies, and therefore the
#: effective ceiling of a parent that declared none.
_DEFAULT_AUTONOMY = "supervised"


# ---------------------------------------------------------------------------
# Platform default execution limits (from docs/platform/policy.md)
# ---------------------------------------------------------------------------
_PLATFORM_DEFAULT_LIMITS: dict[str, int] = {
    "tool_call_timeout_s": 10,
    "total_execution_timeout_s": 60,
    "max_tool_calls": 20,
    # #289: tool errors (a raising tool, a tool timeout) a turn tolerates;
    # reaching it ends the turn with a controlled reply.
    "max_tool_errors": 3,
    "max_delegation_depth": 2,
    "max_clarification_attempts": 3,
    # Issue #28: estimated tokens a model call may carry (system prompt,
    # tool schemas and history); the oldest history is trimmed past it. The
    # default is a share of an assumed context window, see agent/context.py.
    "max_context_tokens": DEFAULT_MAX_CONTEXT_TOKENS,
}


#: Public alias of ``_PLATFORM_DEFAULT_LIMITS`` — the Agent Runtime (D-014 S2,
#: design AD-3) merges a runtime's ``execution_limits`` over this constant when
#: enforcing ``max_tool_calls``/``total_execution_timeout_s``/``tool_call_timeout_s``.
PLATFORM_DEFAULT_LIMITS: dict[str, int] = _PLATFORM_DEFAULT_LIMITS


#: The policy mappings that take ``{inherit: true, ...}`` directives.
POLICY_FIELDS = (
    "escalation_rules",
    "delegation_policy",
    "memory_policy",
    "audit_policy",
)


_DEPLOYMENT_RULE = "Deployments may only restrict, not elevate."


#: `platform/roles/base/policy.md`'s rule, as it applies to a definition the
#: importer wrote (see `_apply_importer_ceiling`).
_IMPORTER_RULE = (
    "An importer agent is additive for capability but subtractive for "
    "safety: it may match or tighten its parent's autonomy, "
    "execution_limits, audit redaction and escalation rules, never loosen, "
    "raise or remove them (a null limit means the platform default)."
)


#: The part of that rule a predefined child role is held to as well (issue
#: #106). The same author writes both files, so it may still choose its own
#: autonomy and limits (`_fold_parent_into_child`).
_FOLD_RULE = (
    "A child role may never drop an escalation condition it inherits or "
    "loosen its parent's audit redaction."
)
