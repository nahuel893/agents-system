"""Tests for audit recorder helpers (T-14, T-15)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class MockDefinition:
    """Minimal stand-in for AgentDefinition for recorder tests."""

    def __init__(
        self,
        role: str = "sales-agent",
        client: str | None = "badie",
    ) -> None:
        self.role_name = role
        self.client = client


class TestRecorderEventFamilies:
    """T-14 RED: Each recorder helper produces the correct event family."""

    @pytest.mark.asyncio
    async def test_record_tool_call_attempted(self) -> None:
        """record_tool_call_attempted produces a ToolCallAttempted event."""
        from agentsys.audit.events import ToolCallAttempted
        from agentsys.audit.recorder import record_tool_call_attempted

        definition = MockDefinition(role="sales-agent", client="badie")
        event = await record_tool_call_attempted(
            definition,
            tool_name="order_writer",
            sensitive=True,
            executed=True,
            elapsed_ms=42.0,
            revalidated=True,
            error=None,
        )
        assert isinstance(event, ToolCallAttempted)
        assert event.tool_name == "order_writer"
        assert event.sensitive is True
        assert event.executed is True
        assert event.elapsed_ms == 42.0
        assert event.revalidated is True
        assert event.role == "sales-agent"
        assert event.deployment == "badie"

    @pytest.mark.asyncio
    async def test_record_tool_call_blocked(self) -> None:
        """record_tool_call_blocked produces a ToolCallBlocked event."""
        from agentsys.audit.events import ToolCallBlocked
        from agentsys.audit.recorder import record_tool_call_blocked

        definition = MockDefinition()
        event = await record_tool_call_blocked(
            definition, tool_name="order_writer", reason="not_in_surface"
        )
        assert isinstance(event, ToolCallBlocked)
        assert event.tool_name == "order_writer"
        assert event.reason == "not_in_surface"

    @pytest.mark.asyncio
    async def test_record_tool_granted(self) -> None:
        """record_tool_granted produces a ToolGranted event."""
        from agentsys.audit.events import ToolGranted
        from agentsys.audit.recorder import record_tool_granted

        definition = MockDefinition()
        event = await record_tool_granted(definition, tool_name="catalog_search")
        assert isinstance(event, ToolGranted)
        assert event.tool_name == "catalog_search"

    @pytest.mark.asyncio
    async def test_record_tool_denied(self) -> None:
        """record_tool_denied produces a ToolDenied event."""
        from agentsys.audit.events import ToolDenied
        from agentsys.audit.recorder import record_tool_denied

        definition = MockDefinition()
        event = await record_tool_denied(
            definition, tool_name="order_writer", reason="no_permission"
        )
        assert isinstance(event, ToolDenied)
        assert event.tool_name == "order_writer"
        assert event.reason == "no_permission"

    @pytest.mark.asyncio
    async def test_record_unknown_tool(self) -> None:
        """record_unknown_tool produces an UnknownTool event."""
        from agentsys.audit.events import UnknownTool
        from agentsys.audit.recorder import record_unknown_tool

        definition = MockDefinition()
        event = await record_unknown_tool(definition, tool_name="nonexistent_tool")
        assert isinstance(event, UnknownTool)
        assert event.tool_name == "nonexistent_tool"

    @pytest.mark.asyncio
    async def test_record_skill_loaded(self) -> None:
        """record_skill_loaded produces a SkillLoaded event."""
        from agentsys.audit.events import SkillLoaded
        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        event = await record_skill_loaded(definition, skill="sales-kb")
        assert isinstance(event, SkillLoaded)
        assert event.skill == "sales-kb"

    @pytest.mark.asyncio
    async def test_record_skill_missing(self) -> None:
        """record_skill_missing produces a SkillMissing event."""
        from agentsys.audit.events import SkillMissing
        from agentsys.audit.recorder import record_skill_missing

        definition = MockDefinition()
        event = await record_skill_missing(
            definition, skill="missing-skill", path="/skills/missing-skill.py"
        )
        assert isinstance(event, SkillMissing)
        assert event.skill == "missing-skill"
        assert event.path == "/skills/missing-skill.py"

    @pytest.mark.asyncio
    async def test_record_runtime_built(self) -> None:
        """record_runtime_built produces a RuntimeBuilt event."""
        from agentsys.audit.events import RuntimeBuilt
        from agentsys.audit.recorder import record_runtime_built

        definition = MockDefinition()
        event = await record_runtime_built(definition, tools_count=5, denied_count=2, skills_count=3)
        assert isinstance(event, RuntimeBuilt)
        assert event.tools == 5
        assert event.denied == 2
        assert event.skills == 3

    @pytest.mark.asyncio
    async def test_record_runtime_initialized(self) -> None:
        """record_runtime_initialized produces a RuntimeInitialized event."""
        from agentsys.audit.events import RuntimeInitialized
        from agentsys.audit.recorder import record_runtime_initialized

        definition = MockDefinition()
        event = await record_runtime_initialized(definition, tools_count=5, model_type="groq")
        assert isinstance(event, RuntimeInitialized)
        assert event.tools == 5
        assert event.model_type == "groq"

    @pytest.mark.asyncio
    async def test_record_runtime_timeout(self) -> None:
        """record_runtime_timeout produces a RuntimeTimeout event."""
        from agentsys.audit.events import RuntimeTimeout
        from agentsys.audit.recorder import record_runtime_timeout

        definition = MockDefinition()
        event = await record_runtime_timeout(definition, total_execution_timeout_s=30.0)
        assert isinstance(event, RuntimeTimeout)
        assert event.total_execution_timeout_s == 30.0


class TestRecorderCorrelationId:
    """correlation_id is captured from structlog.contextvars at call time."""

    @pytest.mark.asyncio
    async def test_correlation_id_from_contextvar(self) -> None:
        """When structlog.contextvars has request_id, event has that correlation_id."""
        import structlog.contextvars

        from agentsys.audit.events import SkillLoaded
        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        # Bind a request_id
        with structlog.contextvars.bound_contextvars(request_id="abc-123-request"):
            event = await record_skill_loaded(definition, skill="sales-kb")
        assert isinstance(event, SkillLoaded)
        assert event.correlation_id == "abc-123-request"

    @pytest.mark.asyncio
    async def test_correlation_id_defaults_to_none_when_no_context(self) -> None:
        """When structlog.contextvars has no request_id, correlation_id is 'none'."""
        from agentsys.audit.events import SkillLoaded
        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        event = await record_skill_loaded(definition, skill="sales-kb")
        assert isinstance(event, SkillLoaded)
        assert event.correlation_id == "none"


class TestRecorderSequence:
    """sequence is auto-assigned from a per-correlation counter."""

    @pytest.mark.asyncio
    async def test_sequence_auto_incremented(self) -> None:
        """Two events for the same correlation_id have different sequence numbers."""
        import structlog.contextvars

        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        with structlog.contextvars.bound_contextvars(request_id="seq-test"):
            event1 = await record_skill_loaded(definition, skill="skill-a")
            event2 = await record_skill_loaded(definition, skill="skill-b")
        assert event1.sequence != event2.sequence
        assert event1.sequence == 1
        assert event2.sequence == 2

    @pytest.mark.asyncio
    async def test_sequence_per_correlation_id(self) -> None:
        """Different correlation_ids have independent sequence counters."""
        import structlog.contextvars

        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        with structlog.contextvars.bound_contextvars(request_id="corr-a"):
            event_a = await record_skill_loaded(definition, skill="skill-a")
        with structlog.contextvars.bound_contextvars(request_id="corr-b"):
            event_b = await record_skill_loaded(definition, skill="skill-b")
        # Both have sequence 1 (independent counters)
        assert event_a.sequence == 1
        assert event_b.sequence == 1


class TestRecorderAutoFields:
    """event_id and occurred_at are auto-assigned; definition is deep-copied."""

    @pytest.mark.asyncio
    async def test_event_id_is_uuid(self) -> None:
        """event_id is a UUID (not all zeros or empty)."""
        from uuid import UUID

        from agentsys.audit.events import SkillLoaded
        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition()
        event = await record_skill_loaded(definition, skill="test-skill")
        assert isinstance(event, SkillLoaded)
        assert isinstance(event.event_id, UUID)
        assert event.event_id != UUID(int=0)

    @pytest.mark.asyncio
    async def test_occurred_at_is_utc(self) -> None:
        """occurred_at is a UTC datetime (tz=timezone.utc)."""
        from agentsys.audit.events import SkillLoaded
        from agentsys.audit.recorder import record_skill_loaded

        before = datetime.now(tz=timezone.utc)
        definition = MockDefinition()
        event = await record_skill_loaded(definition, skill="test-skill")
        after = datetime.now(tz=timezone.utc)
        assert isinstance(event, SkillLoaded)
        assert event.occurred_at.tzinfo == timezone.utc
        assert before <= event.occurred_at <= after

    @pytest.mark.asyncio
    async def test_definition_not_shared(self) -> None:
        """Modifying the definition after recording does not affect the event."""
        from agentsys.audit.recorder import record_skill_loaded

        definition = MockDefinition(role="original-role")
        event = await record_skill_loaded(definition, skill="test-skill")
        # Mutate definition after recording
        definition.role_name = "mutated-role"
        assert event.role == "original-role"
