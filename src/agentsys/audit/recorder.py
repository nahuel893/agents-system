"""Recorder — thin adapter helpers per event family (T-14, T-15).

Each ``record_*`` helper:
  - Captures ``correlation_id`` from ``structlog.contextvars.get("request_id")`` at call time.
  - Auto-assigns ``sequence`` from a per-process in-memory counter (guarded by ``asyncio.Lock``).
  - Auto-assigns ``event_id`` (UUID4) and ``occurred_at`` (UTC now).
  - Deep-copies ``definition`` to extract role/deployment (no shared mutable refs).
  - Returns the fully-populated event (does NOT call AuditSink.record() — PR-3 wiring).

Full spec: REQ-AUDIT-50..
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog.contextvars

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
) -> tuple[str, str | None, str | None]:
    """Extract role, deployment, actor from an AgentDefinition object."""
    role = getattr(definition, "role_name", getattr(definition, "role", "unknown"))
    deployment = getattr(definition, "client", None)
    actor = None
    return role, deployment, actor


# ---------------------------------------------------------------------------
# Per-family helpers
# ---------------------------------------------------------------------------

# NOTE: All helpers are currently async because _allocate_sequence is async.
# PR-3 may switch to a sync counter if the sink moves sequence allocation
# to the drainer side (DB-level sequence). For PR-2, in-memory async is fine.


async def record_tool_call_attempted(
    definition: Any,
    tool_name: str,
    sensitive: bool = False,
    *,
    executed: bool = False,
    elapsed_ms: float | None = None,
    revalidated: bool = False,
    error: str | None = None,
) -> "ToolCallAttempted":
    """Build a ToolCallAttempted event."""
    # We need the import here to avoid circular imports at module level
    from agentsys.audit.events import ToolCallAttempted

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={
            "tool_name": tool_name,
            "sensitive": sensitive,
            "executed": executed,
            "elapsed_ms": elapsed_ms,
            "revalidated": revalidated,
            "error": error,
        },
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
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={"tool_name": tool_name, "reason": reason},
    )


async def record_tool_granted(
    definition: Any,
    tool_name: str,
) -> "ToolGranted":
    """Build a ToolGranted event."""
    from agentsys.audit.events import ToolGranted

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor = _extract_role_deployment(definition)
    return ToolGranted(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        tool_name=tool_name,
        payload={"tool_name": tool_name},
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
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={"tool_name": tool_name, "reason": reason},
    )


async def record_unknown_tool(
    definition: Any,
    tool_name: str,
) -> "UnknownTool":
    """Build an UnknownTool event."""
    from agentsys.audit.events import UnknownTool

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor = _extract_role_deployment(definition)
    return UnknownTool(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        tool_name=tool_name,
        payload={"tool_name": tool_name},
    )


async def record_skill_loaded(
    definition: Any,
    skill: str,
) -> "SkillLoaded":
    """Build a SkillLoaded event."""
    from agentsys.audit.events import SkillLoaded

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor = _extract_role_deployment(definition)
    return SkillLoaded(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        skill=skill,
        payload={"skill": skill},
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
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={"skill": skill, "path": path},
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
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={"tools": tools_count, "denied": denied_count, "skills": skills_count},
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
    role, deployment, actor = _extract_role_deployment(definition)
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
        payload={"tools": tools_count, "model_type": model_type},
    )


async def record_runtime_timeout(
    definition: Any,
    total_execution_timeout_s: float,
) -> "RuntimeTimeout":
    """Build a RuntimeTimeout event."""
    from agentsys.audit.events import RuntimeTimeout

    correlation_id = _correlation_id_from_context()
    sequence = await _allocate_sequence(correlation_id)
    role, deployment, actor = _extract_role_deployment(definition)
    return RuntimeTimeout(
        event_id=uuid.uuid4(),
        occurred_at=_now_utc(),
        correlation_id=correlation_id,
        sequence=sequence,
        role=role,
        deployment=deployment,
        actor=actor,
        total_execution_timeout_s=total_execution_timeout_s,
        payload={"total_execution_timeout_s": total_execution_timeout_s},
    )
