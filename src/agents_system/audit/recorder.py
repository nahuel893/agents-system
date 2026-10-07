"""Recorder — thin adapter helpers per event family (T-14, T-15).

Each ``record_*`` helper:
  - Captures ``correlation_id`` from ``structlog.contextvars.get("request_id")`` at call time.
  - Writes ``sequence`` as ``PLACEHOLDER_SEQUENCE`` — the real value is allocated
    atomically, per event, at flush time by ``AuditSink._flush_batch`` (issue #9).
  - Auto-assigns ``event_id`` (UUID4) and ``occurred_at`` (UTC now).
  - Deep-copies ``definition`` to extract role/deployment (no shared mutable refs).
  - Calls ``Redactor.redact(payload, audit_policy)`` to get (redacted_payload, pii_keys).
  - Calls ``AuditSink.current().record(event)`` with the fully-built event.

Full spec: REQ-AUDIT-50..
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog.contextvars

from agents_system.audit.redactor import Redactor
from agents_system.subject import current_actor, current_subject

if TYPE_CHECKING:
    from agents_system.audit.events import (
        BudgetRefused,
        ContextTrimmed,
        EscalationNotDelivered,
        ProactiveDelivery,
        RuntimeBuilt,
        RuntimeInitialized,
        RuntimeTimeout,
        SkillLoaded,
        SkillMissing,
        SubjectDataDeleted,
        ToolCallAttempted,
        ToolCallBlocked,
        ToolDenied,
        ToolGranted,
        UnknownTool,
    )


# ---------------------------------------------------------------------------
# Sequence placeholder (issue #9, ADR-001 D-042)
# ---------------------------------------------------------------------------
#: Written into every event's ``sequence`` field at construction time, in
#: place of the module-level ``_seq_counter``/``_allocate_sequence`` this
#: recorder used to keep. That in-process counter was correct within one
#: process and wrong across N: every process counted the ``"none"`` fallback
#: ``correlation_id`` (used whenever there is no bound request context) from
#: 1 independently, so two workers emitting a contextless event in the same
#: instant could allocate the same sequence and collide on
#: ``uq_audit_event_correlation_sequence``. There is no in-process fix for
#: that — the counter has to live somewhere every process shares. The real,
#: authoritative sequence is now allocated atomically at flush time, against
#: the ``audit_sequence`` table, by ``AuditSink._flush_batch`` (migration
#: ``005_audit_sequence``). This placeholder is overwritten there before the
#: event is mapped to its ORM row and is never persisted.
PLACEHOLDER_SEQUENCE = 0


def _current_subject_key() -> str | None:
    """The bound turn subject's key. Never its address (ADR-011 part 2)."""
    subject = current_subject()
    return subject.key if subject is not None else None


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _correlation_id_from_context() -> str:
    """Read request_id from structlog contextvars, defaulting to 'none'."""
    return structlog.contextvars.get_contextvars().get("request_id", None) or "none"


def _extract_role_deployment(
    definition: Any,
) -> tuple[str, str | None, str | None, dict[str, Any]]:
    """Extract role, deployment, actor, and audit_policy from an AgentDefinition object."""
    role = getattr(definition, "role_name", "unknown")
    # AgentDefinition names this ``deployment``. Reading ``client`` here matched
    # nothing in src/, so every event built from a real definition recorded the
    # default and the audit trail could not tell deployments apart. Pinned by
    # TestRecorderAgentDefinitionContract::test_deployment_from_real_definition.
    deployment = getattr(definition, "deployment", None)
    bound_actor = current_actor()
    actor = bound_actor.label if bound_actor is not None else None
    audit_policy = getattr(definition, "audit_policy", {}) or {}
    return role, deployment, actor, audit_policy


def _build_and_redact(
    payload: dict[str, Any],
    audit_policy: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Build payload and redact it via Redactor.

    Returns (redacted_payload, pii_keys).
    """
    redactor = Redactor()
    return redactor.redact(payload, audit_policy)


# ---------------------------------------------------------------------------
# Per-family helpers
# ---------------------------------------------------------------------------

# NOTE: All helpers stay async for call-site compatibility (`await record_*(...)`
# throughout the harness), even though none of them awaits anything internally
# now that sequence allocation moved out of the recorder (issue #9).


async def record_tool_call_attempted(
    definition: Any,
    tool_name: str,
    sensitive: bool = False,
    *,
    executed: bool = False,
    elapsed_ms: float | None = None,
    revalidated: bool = False,
    error: str | None = None,
    tool_input: dict[str, Any] | None = None,
    builtin_default: bool = False,
) -> ToolCallAttempted:
    """Build a ToolCallAttempted event."""
    from agents_system.audit.events import ToolCallAttempted

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload: dict[str, Any] = {
        "tool_name": tool_name,
        "sensitive": sensitive,
        "executed": executed,
        "elapsed_ms": elapsed_ms,
        "revalidated": revalidated,
        "error": error,
        "builtin_default": builtin_default,
    }
    if tool_input is not None:
        raw_payload["tool_input"] = tool_input

    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ToolCallAttempted(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        sensitive=sensitive,
        executed=executed,
        elapsed_ms=elapsed_ms,
        revalidated=revalidated,
        error=error,
        builtin_default=builtin_default,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_call_blocked(
    definition: Any,
    tool_name: str,
    reason: str,
) -> ToolCallBlocked:
    """Build a ToolCallBlocked event."""
    from agents_system.audit.events import ToolCallBlocked

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tool_name": tool_name, "reason": reason}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ToolCallBlocked(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        reason=reason,  # type: ignore[arg-type]
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_granted(
    definition: Any,
    tool_name: str,
    *,
    builtin_default: bool = False,
) -> ToolGranted:
    """Build a ToolGranted event."""
    from agents_system.audit.events import ToolGranted

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tool_name": tool_name, "builtin_default": builtin_default}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ToolGranted(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        builtin_default=builtin_default,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_denied(
    definition: Any,
    tool_name: str,
    reason: str,
) -> ToolDenied:
    """Build a ToolDenied event."""
    from agents_system.audit.events import ToolDenied

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tool_name": tool_name, "reason": reason}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ToolDenied(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        reason=reason,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_unknown_tool(
    definition: Any,
    tool_name: str,
) -> UnknownTool:
    """Build an UnknownTool event."""
    from agents_system.audit.events import UnknownTool

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tool_name": tool_name}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return UnknownTool(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_skill_loaded(
    definition: Any,
    skill: str,
) -> SkillLoaded:
    """Build a SkillLoaded event."""
    from agents_system.audit.events import SkillLoaded

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"skill": skill}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return SkillLoaded(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        skill=skill,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_skill_missing(
    definition: Any,
    skill: str,
    path: str,
) -> SkillMissing:
    """Build a SkillMissing event."""
    from agents_system.audit.events import SkillMissing

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"skill": skill, "path": path}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return SkillMissing(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        skill=skill,
        path=path,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_runtime_built(
    definition: Any,
    tools_count: int,
    denied_count: int,
    skills_count: int,
    builtin_defaults: tuple[str, ...] = (),
) -> RuntimeBuilt:
    """Build a RuntimeBuilt event."""
    from agents_system.audit.events import RuntimeBuilt

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {
        "tools": tools_count,
        "denied": denied_count,
        "skills": skills_count,
        "builtin_defaults": list(builtin_defaults),
    }
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return RuntimeBuilt(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tools=tools_count,
        denied=denied_count,
        skills=skills_count,
        builtin_defaults=list(builtin_defaults),
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_runtime_initialized(
    definition: Any,
    tools_count: int,
    model_type: str,
) -> RuntimeInitialized:
    """Build a RuntimeInitialized event."""
    from agents_system.audit.events import RuntimeInitialized

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tools": tools_count, "model_type": model_type}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return RuntimeInitialized(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tools=tools_count,
        model_type=model_type,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_runtime_timeout(
    definition: Any,
    total_execution_timeout_s: float,
) -> RuntimeTimeout:
    """Build a RuntimeTimeout event."""
    from agents_system.audit.events import RuntimeTimeout

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"total_execution_timeout_s": total_execution_timeout_s}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return RuntimeTimeout(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        total_execution_timeout_s=total_execution_timeout_s,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_context_trimmed(
    definition: Any,
    dropped_messages: int,
    kept_messages: int,
    tokens_before: int,
    tokens_after: int,
    max_context_tokens: int,
    over_budget: bool = False,
) -> ContextTrimmed:
    """Build a ContextTrimmed event (issue #28). Counts only, no message text."""
    from agents_system.audit.events import ContextTrimmed

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {
        "dropped_messages": dropped_messages,
        "kept_messages": kept_messages,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "max_context_tokens": max_context_tokens,
        "over_budget": over_budget,
    }
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ContextTrimmed(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        dropped_messages=dropped_messages,
        kept_messages=kept_messages,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        max_context_tokens=max_context_tokens,
        over_budget=over_budget,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_budget_refused(
    definition: Any,
    *,
    runtime_id: str,
    cap: str,
    limit: float,
    used: float,
) -> BudgetRefused:
    """Build a BudgetRefused event (ADR-011 part 5, #252). Counts only, no text."""
    from agents_system.audit.events import BudgetRefused

    role, deployment, actor, audit_policy = _extract_role_deployment(definition)
    payload, pii_keys = _build_and_redact(
        {"runtime_id": runtime_id, "cap": cap, "limit": limit, "used": used},
        audit_policy,
    )
    return BudgetRefused(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=_correlation_id_from_context(),
        sequence=PLACEHOLDER_SEQUENCE,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        payload=payload,
        pii_keys=pii_keys,
    )


#: The model writes an escalation's ``reason``, so it is untrusted and
#: unbounded. The tool asks for a short label (``customer_angry``); anything
#: past this is cut before it is redacted, stored or logged.
ESCALATION_REASON_MAX_CHARS = 200


def bound_escalation_reason(reason: str) -> str:
    """``reason`` cut to ``ESCALATION_REASON_MAX_CHARS``."""
    return reason[:ESCALATION_REASON_MAX_CHARS]


async def record_escalation_not_delivered(
    definition: Any,
    tool_name: str,
    reason: str,
    details_chars: int,
) -> EscalationNotDelivered:
    """Build an EscalationNotDelivered event (issue #168).

    The escalation's ``details`` are free text about the customer (names,
    IDs, addresses), which the Redactor has no pattern for, so only their
    length is recorded, never their text. ``reason`` is capped and goes
    through the redactor, and the event's top-level ``reason`` field is the
    redacted value, not the raw one.
    """
    from agents_system.audit.events import EscalationNotDelivered

    correlation_id = _correlation_id_from_context()
    sequence = PLACEHOLDER_SEQUENCE
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {
        "tool_name": tool_name,
        "reason": bound_escalation_reason(reason),
        "details_chars": details_chars,
    }
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return EscalationNotDelivered(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        subject=_current_subject_key(),
        tool_name=tool_name,
        reason=redacted_payload["reason"],
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_subject_data_deleted(
    *,
    runtime_id: str,
    subject_key: str,
    actor: str,
    stores: dict[str, int],
    threads: int,
    failed: list[str],
) -> SubjectDataDeleted:
    """Build a SubjectDataDeleted event (ADR-011 part 6, #257).

    Takes the actor and subject explicitly: an operator call is not a turn, so
    no turn scope is bound. Only counts and store names are recorded.
    """
    from agents_system.audit.events import SubjectDataDeleted

    payload = {
        "runtime_id": runtime_id,
        "stores": dict(stores),
        "threads": threads,
        "failed": list(failed),
    }
    return SubjectDataDeleted(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=_correlation_id_from_context(),
        sequence=PLACEHOLDER_SEQUENCE,
        role="platform",
        actor=actor,
        subject=subject_key,
        payload=payload,
        pii_keys=[],
    )


async def record_proactive_delivery(
    *,
    outcome: str,
    runtime_id: str,
    subject_key: str,
    actor: str,
    message_id: str,
    channel: str | None = None,
    reason: str | None = None,
) -> ProactiveDelivery:
    """Build a ProactiveDelivery event (ADR-011 part 5, #251).

    A proactive message is not a turn, so actor and subject are explicit.
    Only the outcome, channel name and a reason code are recorded: never the
    address (it can be reassigned) or the message text.
    """
    from agents_system.audit.events import ProactiveDelivery

    payload: dict[str, Any] = {
        "outcome": outcome,
        "runtime_id": runtime_id,
        "message_id": message_id,
    }
    if channel is not None:
        payload["channel"] = channel
    if reason is not None:
        payload["reason"] = reason
    return ProactiveDelivery(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=_correlation_id_from_context(),
        sequence=PLACEHOLDER_SEQUENCE,
        role="platform",
        actor=actor,
        subject=subject_key,
        payload=payload,
        pii_keys=[],
    )
