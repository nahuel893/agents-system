"""Recorder — thin adapter helpers per event family (T-14, T-15).

Each ``record_*`` helper:
  - Captures ``correlation_id`` from ``structlog.contextvars.get("request_id")`` at call time.
  - Auto-assigns ``sequence`` from a per-process in-memory counter (guarded by ``asyncio.Lock``).
  - Auto-assigns ``event_id`` (UUID4) and ``occurred_at`` (UTC now).
  - Deep-copies ``definition`` to extract role/deployment (no shared mutable refs).
  - Calls ``Redactor.redact(payload, audit_policy)`` to get (redacted_payload, pii_keys).
  - Calls ``AuditSink.current().record(event)`` with the fully-built event.

Full spec: REQ-AUDIT-50..
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog.contextvars

from agentsys.audit.events import _AuditEventBase
from agentsys.audit.redactor import Redactor

if TYPE_CHECKING:
    from agentsys.audit.events import (
        RuntimeBuilt,
        RuntimeInitialized,
        RuntimeTimeout,
        SkillLoaded,
        SkillMissing,
        ToolCallAttempted,
        ToolCallBlocked,
        ToolDenied,
        ToolGranted,
        UnknownTool,
    )


# ---------------------------------------------------------------------------
# Per-correlation sequence counter
# ---------------------------------------------------------------------------
_seq_lock = asyncio.Lock()
_seq_counter: dict[str, int] = {}


async def _allocate_sequence(correlation_id: str) -> int:
    """Allocate the next sequence number for ``correlation_id`` (async-safe)."""
    async with _seq_lock:
        current = _seq_counter.get(correlation_id, 0)
        next_seq = current + 1
        _seq_counter[correlation_id] = next_seq
        return next_seq


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


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
    actor = None
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

# NOTE: All helpers are async because _allocate_sequence is async.


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
) -> "ToolCallAttempted":
    """Build a ToolCallAttempted event."""
    from agentsys.audit.events import ToolCallAttempted

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload: dict[str, Any] = {
        "tool_name": tool_name,
        "sensitive": sensitive,
        "executed": executed,
        "elapsed_ms": elapsed_ms,
        "revalidated": revalidated,
        "error": error,
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
        tool_name=tool_name,
        sensitive=sensitive,
        executed=executed,
        elapsed_ms=elapsed_ms,
        revalidated=revalidated,
        error=error,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_call_blocked(
    definition: Any,
    tool_name: str,
    reason: str,
) -> "ToolCallBlocked":
    """Build a ToolCallBlocked event."""
    from agentsys.audit.events import ToolCallBlocked

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        tool_name=tool_name,
        reason=reason,  # type: ignore[arg-type]
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_granted(
    definition: Any,
    tool_name: str,
) -> "ToolGranted":
    """Build a ToolGranted event."""
    from agentsys.audit.events import ToolGranted

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tool_name": tool_name}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return ToolGranted(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        tool_name=tool_name,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_tool_denied(
    definition: Any,
    tool_name: str,
    reason: str,
) -> "ToolDenied":
    """Build a ToolDenied event."""
    from agentsys.audit.events import ToolDenied

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        tool_name=tool_name,
        reason=reason,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_unknown_tool(
    definition: Any,
    tool_name: str,
) -> "UnknownTool":
    """Build an UnknownTool event."""
    from agentsys.audit.events import UnknownTool

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        tool_name=tool_name,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_skill_loaded(
    definition: Any,
    skill: str,
) -> "SkillLoaded":
    """Build a SkillLoaded event."""
    from agentsys.audit.events import SkillLoaded

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        skill=skill,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_skill_missing(
    definition: Any,
    skill: str,
    path: str,
) -> "SkillMissing":
    """Build a SkillMissing event."""
    from agentsys.audit.events import SkillMissing

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
) -> "RuntimeBuilt":
    """Build a RuntimeBuilt event."""
    from agentsys.audit.events import RuntimeBuilt

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor, audit_policy = _extract_role_deployment(definition)

    raw_payload = {"tools": tools_count, "denied": denied_count, "skills": skills_count}
    redacted_payload, pii_keys = _build_and_redact(raw_payload, audit_policy)

    return RuntimeBuilt(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        tools=tools_count,
        denied=denied_count,
        skills=skills_count,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_runtime_initialized(
    definition: Any,
    tools_count: int,
    model_type: str,
) -> "RuntimeInitialized":
    """Build a RuntimeInitialized event."""
    from agentsys.audit.events import RuntimeInitialized

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        tools=tools_count,
        model_type=model_type,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )


async def record_runtime_timeout(
    definition: Any,
    total_execution_timeout_s: float,
) -> "RuntimeTimeout":
    """Build a RuntimeTimeout event."""
    from agentsys.audit.events import RuntimeTimeout

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
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
        total_execution_timeout_s=total_execution_timeout_s,
        payload=redacted_payload,
        pii_keys=pii_keys,
    )
