"""The platform's reference tools, in one registry (issue #312).

The served app's default registry (``server.default_registry_factory``)
registers these, and ``agents check`` describes an agent against them when it
is given no registry of its own. Every connector is built unbound: it fails
closed when called, so the registry says which tools exist, not that they
reach a backend.

Importing this module needs only the core install; the report tool, which
needs the ``postgres`` extra, is added by ``server.default_registry_factory``
and, when that extra is installed, by ``agents check``.
"""

from __future__ import annotations

from typing import Any

from agents_system.connectors.order_connector import build_order_writer_tool_spec
from agents_system.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agents_system.harness.registry import ToolRegistry


def build_reference_registry(settings: Any = None) -> ToolRegistry:
    """A ``ToolRegistry`` holding the platform's reference tools, unbound.

    ``settings`` (the served app's ``Settings``) sizes the per-user memory
    and schedule tools; ``None`` uses their defaults.
    """
    registry = ToolRegistry()
    registry.register(build_knowledge_retrieval_tool_spec(None))
    registry.register(build_conversation_summarizer_tool_spec(None))
    registry.register(build_escalation_notifier_tool_spec(None))
    registry.register(build_order_writer_tool_spec(None))
    # #250: inert unless a role names them, its memory_policy opts in and the
    # deployment grants read:user_memory / write:user_memory.
    from agents_system.user_memory import (
        UserMemoryLimits,
        build_user_memory_tool_specs,
        limits_from_settings,
    )

    for spec in build_user_memory_tool_specs(
        limits_from_settings(settings) if settings is not None else UserMemoryLimits()
    ):
        registry.register(spec)
    # #175: inert unless a role names them and the deployment grants
    # write:schedule (T2, needs a DEPLOY_GRANTS entry).
    from agents_system.schedules import (
        ScheduleLimits,
        build_schedule_tool_specs,
    )
    from agents_system.schedules import limits_from_settings as schedule_limits

    for spec in build_schedule_tool_specs(
        schedule_limits(settings) if settings is not None else ScheduleLimits()
    ):
        registry.register(spec)
    return registry
