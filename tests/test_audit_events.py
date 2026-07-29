"""Tests for audit event discriminated union (T-08, T-09, T-10)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError


class TestAuditEventDiscriminatedUnion:
    """T-08 RED: AuditEvent discriminated union shape and dispatch."""

    def test_tool_call_attempted_dispatch(self) -> None:
        """Given a valid tool_call_attempted payload, model_validate returns ToolCallAttempted."""
        from agentsys.audit.events import AuditEvent, ToolCallAttempted

        payload = {
            "event_type": "tool_call_attempted",
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "correlation_id": "req-abc-123",
            "sequence": 1,
            "role": "sales-agent",
            "deployment": "badie",
            "actor": None,
            "tool_name": "order_writer",
            "sensitive": False,
            "executed": True,
            "elapsed_ms": 42.5,
            "revalidated": False,
            "error": None,
        }
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, ToolCallAttempted)
        assert event.tool_name == "order_writer"
        assert event.executed is True
        assert event.elapsed_ms == 42.5

    def test_tool_call_blocked_dispatch(self) -> None:
        """Given a valid tool_call_blocked payload, model_validate returns ToolCallBlocked."""
        from agentsys.audit.events import AuditEvent, ToolCallBlocked

        payload = {
            "event_type": "tool_call_blocked",
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "correlation_id": "req-abc-123",
            "sequence": 2,
            "role": "sales-agent",
            "deployment": "badie",
            "actor": None,
            "tool_name": "order_writer",
            "reason": "not_in_surface",
        }
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, ToolCallBlocked)
        assert event.tool_name == "order_writer"
        assert event.reason == "not_in_surface"

    def test_unknown_event_type_raises_validation_error(self) -> None:
        """Given an unknown event_type, model_validate raises ValidationError."""
        from agentsys.audit.events import AuditEvent

        payload = {
            "event_type": "wat",
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "correlation_id": "req-abc-123",
            "sequence": 1,
            "role": "sales-agent",
            "deployment": None,
            "actor": None,
        }
        with pytest.raises(ValidationError):
            AuditEvent.model_validate(payload)

    def test_tool_call_attempted_serialization_preserves_event_type(self) -> None:
        """Given a ToolCallAttempted model, model_dump preserves event_type."""
        from agentsys.audit.events import ToolCallAttempted

        event = ToolCallAttempted(
            event_id=uuid4(),
            occurred_at=datetime.now(tz=timezone.utc),
            correlation_id="req-abc-123",
            sequence=1,
            role="sales-agent",
            deployment="badie",
            actor=None,
            tool_name="catalog_search",
            sensitive=True,
            executed=False,
            elapsed_ms=None,
            revalidated=True,
            error=None,
        )
        data = event.model_dump()
        assert data["event_type"] == "tool_call_attempted"


class TestAllEventFamilies:
    """T-09 GREEN: All 11 sub-models are importable and constructable."""

    def _full_payload(self, event_type: str, **overrides: object) -> dict:
        base = {
            "event_type": event_type,
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "correlation_id": "req-test-001",
            "sequence": 1,
            "role": "test-role",
            "deployment": "test-client",
            "actor": None,
        }
        base.update(overrides)
        return base

    def test_tool_granted(self) -> None:
        from agentsys.audit.events import AuditEvent, ToolGranted

        payload = self._full_payload("tool_granted", tool_name="catalog_search")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, ToolGranted)
        assert event.tool_name == "catalog_search"

    def test_tool_denied(self) -> None:
        from agentsys.audit.events import AuditEvent, ToolDenied

        payload = self._full_payload("tool_denied", tool_name="order_writer", reason="no_permission")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, ToolDenied)
        assert event.tool_name == "order_writer"
        assert event.reason == "no_permission"

    def test_unknown_tool(self) -> None:
        from agentsys.audit.events import AuditEvent, UnknownTool

        payload = self._full_payload("unknown_tool", tool_name="nonexistent_tool")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, UnknownTool)
        assert event.tool_name == "nonexistent_tool"

    def test_skill_loaded(self) -> None:
        from agentsys.audit.events import AuditEvent, SkillLoaded

        payload = self._full_payload("skill_loaded", skill="sales-kb")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, SkillLoaded)
        assert event.skill == "sales-kb"

    def test_skill_missing(self) -> None:
        from agentsys.audit.events import AuditEvent, SkillMissing

        payload = self._full_payload("skill_missing", skill="missing-skill", path="/skills/missing-skill.py")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, SkillMissing)
        assert event.skill == "missing-skill"
        assert event.path == "/skills/missing-skill.py"

    def test_runtime_built(self) -> None:
        from agentsys.audit.events import AuditEvent, RuntimeBuilt

        payload = self._full_payload("runtime_built", tools=5, denied=2, skills=3)
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, RuntimeBuilt)
        assert event.tools == 5
        assert event.denied == 2
        assert event.skills == 3

    def test_runtime_initialized(self) -> None:
        from agentsys.audit.events import AuditEvent, RuntimeInitialized

        payload = self._full_payload("runtime_initialized", tools=5, model_type="groq")
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, RuntimeInitialized)
        assert event.tools == 5
        assert event.model_type == "groq"

    def test_runtime_timeout(self) -> None:
        from agentsys.audit.events import AuditEvent, RuntimeTimeout

        payload = self._full_payload("runtime_timeout", total_execution_timeout_s=30.0)
        event = AuditEvent.model_validate(payload)
        assert isinstance(event, RuntimeTimeout)
        assert event.total_execution_timeout_s == 30.0


class TestAuditEventPayload:
    """T-16 RED: payload field must exist on all event sub-models (REQ-AUDIT-31)."""

    def test_tool_call_attempted_has_payload_field(self) -> None:
        """ToolCallAttempted.payload exists and is a dict (REQ-AUDIT-31)."""
        from agentsys.audit.events import ToolCallAttempted

        event = ToolCallAttempted(
            event_id=uuid4(),
            occurred_at=datetime.now(timezone.utc),
            correlation_id="abc",
            sequence=1,
            role="sales-agent",
            tool_name="order_writer",
            sensitive=True,
            executed=True,
            elapsed_ms=42.5,
            revalidated=True,
            error=None,
        )
        # payload must exist and be a dict (populated by recorder, defaults to {} on direct ctor)
        assert hasattr(event, "payload")
        assert isinstance(event.payload, dict)

    def test_recorder_populates_payload(self) -> None:
        """Recorder helpers must populate payload with event-specific data (REQ-AUDIT-31)."""
        import asyncio
        from agentsys.audit.events import ToolCallAttempted
        from agentsys.audit.recorder import record_tool_call_attempted

        class FakeDef:
            role_name = "sales-agent"
            client = "badie"

        event = asyncio.run(
            record_tool_call_attempted(
                FakeDef(),
                tool_name="order_writer",
                sensitive=True,
                executed=True,
                elapsed_ms=42.5,
                revalidated=True,
                error=None,
            )
        )
        assert isinstance(event, ToolCallAttempted)
        assert event.payload == {
            "tool_name": "order_writer",
            "sensitive": True,
            "executed": True,
            "elapsed_ms": 42.5,
            "revalidated": True,
            "error": None,
        }

    def test_audit_event_model_validate_with_payload_round_trip(self) -> None:
        """AuditEvent.model_validate then model_dump includes payload (REQ-AUDIT-31)."""
        from agentsys.audit.events import AuditEvent

        data = {
            "event_type": "skill_loaded",
            "event_id": str(uuid4()),
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "correlation_id": "req-abc",
            "sequence": 1,
            "role": "sales-agent",
            "deployment": "badie",
            "actor": None,
            "skill": "sales-kb",
            "payload": {"skill": "sales-kb"},
        }
        event = AuditEvent.model_validate(data)
        dumped = event.model_dump()
        assert "payload" in dumped
        assert dumped["payload"] == {"skill": "sales-kb"}

    def test_map_to_audit_event_no_key_error(self) -> None:
        """T-18: round-trip ToolCallAttempted → model_dump → map_to_audit_event (C4).

        map_to_audit_event accesses event_data['payload'] directly.
        If payload is absent from model_dump(), this raises KeyError.
        """
        from agentsys.audit.events import ToolCallAttempted
        from agentsys.models.audit_event import map_to_audit_event

        event = ToolCallAttempted(
            event_id=uuid4(),
            occurred_at=datetime.now(timezone.utc),
            correlation_id="req-test",
            sequence=1,
            role="sales-agent",
            tool_name="catalog_search",
            sensitive=False,
            executed=True,
            elapsed_ms=10.0,
            revalidated=False,
            error=None,
        )
        data = event.model_dump()
        # Must not raise KeyError: 'payload'
        orm_row = map_to_audit_event(data)
        assert orm_row.payload is not None
        assert isinstance(orm_row.payload, dict)
