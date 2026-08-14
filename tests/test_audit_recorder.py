"""Tests for audit recorder helpers (T-14, T-15)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentsys.harness.loader import AgentDefinition


class MockDefinition:
    """Minimal stand-in for AgentDefinition for recorder tests.

    Field names mirror the real ``agentsys.harness.loader.AgentDefinition``.
    They did not: this mock used to invent a ``client`` field, which is what
    hid the recorder reading an attribute no real definition has. A mock that
    diverges from the type it stands in for tests the mock, not the code.
    """

    def __init__(
        self,
        role: str = "sales-agent",
        deployment: str | None = "badie",
    ) -> None:
        self.role_name = role
        self.deployment = deployment


def real_definition(
    *,
    role_name: str = "sales-agent",
    deployment: str | None = "badie",
    audit_policy: dict[str, object] | None = None,
) -> AgentDefinition:
    """Build the actual frozen value object the recorder is called with."""
    return AgentDefinition(
        role_name=role_name,
        version="1.0",
        deployment=deployment,
        system_prompt="prompt",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy=audit_policy or {},
        execution_limits=None,
    )


class TestRecorderEventFamilies:
    """T-14 RED: Each recorder helper produces the correct event family."""

    @pytest.mark.asyncio
    async def test_record_tool_call_attempted(self) -> None:
        """record_tool_call_attempted produces a ToolCallAttempted event."""
        from agentsys.audit.events import ToolCallAttempted
        from agentsys.audit.recorder import record_tool_call_attempted

        definition = MockDefinition(role="sales-agent", deployment="badie")
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


class TestRecorderIdentityExtraction:
    """``_extract_role_deployment`` resolves agent identity from the definition.

    Replaces the previous ``test_definition_not_shared``, which mutated
    ``definition.role_name`` — a ``str`` rebind that cannot propagate to an
    already-extracted value under ANY implementation, so it passed for free and
    certified a deep-copy claim the recorder does not implement (``recorder.py``
    never imports ``copy``).
    """

    @pytest.mark.asyncio
    async def test_role_read_from_role_name(self) -> None:
        """role_name is the primary source for the event's role."""
        from agentsys.audit.recorder import record_skill_loaded

        event = await record_skill_loaded(MockDefinition(role="sales-agent"), skill="s")
        assert event.role == "sales-agent"


    @pytest.mark.asyncio
    async def test_role_falls_back_to_unknown(self) -> None:
        """A definition with no role attribute never crashes the recorder."""
        from agentsys.audit.recorder import record_skill_loaded

        class Nameless:
            pass

        event = await record_skill_loaded(Nameless(), skill="s")
        assert event.role == "unknown"

    @pytest.mark.asyncio
    async def test_deployment_is_none_when_absent(self) -> None:
        """A definition without a deployment records None, not a crash."""
        from agentsys.audit.recorder import record_skill_loaded

        event = await record_skill_loaded(MockDefinition(deployment=None), skill="s")
        assert event.deployment is None


class TestRecorderAgentDefinitionContract:
    """The recorder must work against the REAL value object, not the mock.

    ``agentsys.harness.loader.AgentDefinition`` is a frozen dataclass whose
    fields are ``role_name`` and ``deployment``. It has no ``client`` field —
    nothing in ``src/`` does. The other tests in this file pass a mock that
    invents one, which is why the divergence went unnoticed.
    """

    @pytest.mark.asyncio
    async def test_role_from_real_definition(self) -> None:
        """role_name matches on the real object."""
        from agentsys.audit.recorder import record_skill_loaded

        event = await record_skill_loaded(real_definition(), skill="sales-kb")
        assert event.role == "sales-agent"

    @pytest.mark.asyncio
    async def test_deployment_from_real_definition(self) -> None:
        """The deployment must survive onto the event."""
        from agentsys.audit.recorder import record_skill_loaded

        event = await record_skill_loaded(real_definition(deployment="badie"), skill="sales-kb")
        assert event.deployment == "badie"


class TestRecorderRedaction:
    """Payloads must be redacted before they leave the recorder.

    ``recorder.py`` never imports ``Redactor``, so every payload it emits today
    is raw. Since the PR-3 sink is not written yet, nothing else forces the
    redaction step to exist — these tests are the forcing function.
    """

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason="recorder.py never calls Redactor, so free-text reasons reach the payload raw",
    )
    async def test_tool_denied_reason_is_redacted(self) -> None:
        """A denial reason quoting a customer phone must not be stored verbatim."""
        from agentsys.audit.recorder import record_tool_denied

        event = await record_tool_denied(
            real_definition(),
            tool_name="order_writer",
            reason="no client for +5491123456789",
        )
        assert "+5491123456789" not in repr(event.payload)

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        strict=True,
        reason="recorder.py never calls Redactor, so DB error text reaches the payload raw",
    )
    async def test_tool_call_error_is_redacted(self) -> None:
        """A driver error echoing customer data must not be stored verbatim."""
        from agentsys.audit.recorder import record_tool_call_attempted

        event = await record_tool_call_attempted(
            real_definition(),
            tool_name="order_writer",
            error="duplicate key: (phone)=(+5491123456789) already exists",
        )
        assert "+5491123456789" not in repr(event.payload)
