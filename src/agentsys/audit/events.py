"""Pydantic discriminated union for all audit event families (REQ-AUDIT-10, 11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError


class _AuditEventBase(BaseModel):
    """Shared fields for all audit event sub-models."""

    event_id: UUID
    occurred_at: datetime
    correlation_id: str
    sequence: int
    role: str
    deployment: str | None = None
    actor: str | None = None


class ToolCallAttempted(_AuditEventBase):
    """Emitted when an attempt is made to call a tool (allowed or blocked)."""

    event_type: Literal["tool_call_attempted"] = "tool_call_attempted"
    tool_name: str
    sensitive: bool = False
    executed: bool = False
    elapsed_ms: float | None = None
    revalidated: bool = False
    error: str | None = None


class ToolCallBlocked(_AuditEventBase):
    """Emitted when a tool call is blocked at runtime (interceptor layer)."""

    event_type: Literal["tool_call_blocked"] = "tool_call_blocked"
    tool_name: str
    reason: Literal["not_in_surface", "revalidation_required", "permission_revoked"]


class ToolGranted(_AuditEventBase):
    """Emitted when a tool is granted to the runtime at build time (injector layer)."""

    event_type: Literal["tool_granted"] = "tool_granted"
    tool_name: str


class ToolDenied(_AuditEventBase):
    """Emitted when a tool is denied at build time (injector layer)."""

    event_type: Literal["tool_denied"] = "tool_denied"
    tool_name: str
    reason: str


class UnknownTool(_AuditEventBase):
    """Emitted when an unknown tool name is encountered at build time."""

    event_type: Literal["unknown_tool"] = "unknown_tool"
    tool_name: str


class SkillLoaded(_AuditEventBase):
    """Emitted when a skill file is successfully loaded at build time."""

    event_type: Literal["skill_loaded"] = "skill_loaded"
    skill: str


class SkillMissing(_AuditEventBase):
    """Emitted when a skill file is not found at build time."""

    event_type: Literal["skill_missing"] = "skill_missing"
    skill: str
    path: str


class RuntimeBuilt(_AuditEventBase):
    """Emitted after the runtime is built (tools + skills resolved)."""

    event_type: Literal["runtime_built"] = "runtime_built"
    tools: int
    denied: int
    skills: int


class RuntimeInitialized(_AuditEventBase):
    """Emitted when the agent runtime is initialized for a turn."""

    event_type: Literal["runtime_initialized"] = "runtime_initialized"
    tools: int
    model_type: str


class RuntimeTimeout(_AuditEventBase):
    """Emitted when a turn hits the execution timeout."""

    event_type: Literal["runtime_timeout"] = "runtime_timeout"
    total_execution_timeout_s: float


# Discriminator dispatch table — used by AuditEvent.model_validate
_EVENT_DISPATCH: dict[str, type[_AuditEventBase]] = {
    "tool_call_attempted": ToolCallAttempted,
    "tool_call_blocked": ToolCallBlocked,
    "tool_granted": ToolGranted,
    "tool_denied": ToolDenied,
    "unknown_tool": UnknownTool,
    "skill_loaded": SkillLoaded,
    "skill_missing": SkillMissing,
    "runtime_built": RuntimeBuilt,
    "runtime_initialized": RuntimeInitialized,
    "runtime_timeout": RuntimeTimeout,
}


class AuditEvent:
    """Discriminated union of all audit event families keyed on ``event_type``.

    ``AuditEvent.model_validate(data)`` dispatches to the correct submodel and
    returns an instance of that submodel (e.g. ``ToolCallAttempted``), so
    ``isinstance(event, ToolCallAttempted)`` is ``True``.

    Unknown ``event_type`` values raise ``pydantic.ValidationError``.
    """

    @classmethod
    def model_validate(cls, data: Any) -> _AuditEventBase:
        """Validate ``data`` dict, dispatch to the correct submodel, return it."""
        if not isinstance(data, dict):
            # Non-dict inputs: raise a ValidationError
            raise ValidationError.from_exception_data(
                title="AuditEvent",
                line_errors=[
                    {
                        "type": "model_type",
                        "input": data,
                        "loc": (),
                        "ctx": {"class_name": "AuditEvent"},
                    },
                ],
            )
        event_type = data.get("event_type")
        if event_type is None:
            raise ValueError("event_type is required")
        if event_type not in _EVENT_DISPATCH:
            # Raise ValidationError by attempting validation against any submodel
            # (all will fail for unknown event_type, triggering a proper ValidationError)
            errors: list[Exception] = []
            for subcls in _EVENT_DISPATCH.values():
                try:
                    subcls.model_validate(data)
                except Exception as exc:  # noqa: PERF203
                    errors.append(exc)
            if errors:
                # Re-raise the first one as a pydantic.ValidationError if it's one
                if isinstance(errors[0], ValidationError):
                    raise errors[0]
                # Fallback: raise a generic ValidationError
                raise ValidationError.from_exception_data(
                    title="AuditEvent",
                    line_errors=[
                        {
                            "type": "literal_error",
                            "input": data,
                            "loc": ("event_type",),
                            "ctx": {"expected": event_type},
                        },
                    ],
                )
        return _EVENT_DISPATCH[event_type].model_validate(data)


# Patch AuditEvent so it "is" the union for type-checkers' purposes
AuditEvent.model_validate.__annotations__["return"] = "_AuditEventBase"
