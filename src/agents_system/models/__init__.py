"""Database models — re-export for convenience.

Importing this package registers EVERY table in ``Base.metadata``. That is a
requirement, not a convenience: while ``audit_event`` was reachable only by
importing ``agents_system.models.audit_event`` directly, the metadata held one
table or two depending on whether some earlier module happened to import it.
Test assertions about the table inventory then passed alone and failed in a
full run, and ``create_all`` raised or not for the same reason.

Alembic owns the platform tables declared here. A deployment that adds its own
tables must import them the same way to keep the metadata inventory stable.
"""

from agents_system.models.audit_event import AuditEvent, map_to_audit_event
from agents_system.models.base import (
    Base,
    alembic_owned_tables,
    get_db,
    get_engine,
    get_session_factory,
)
from agents_system.models.outbox import InboundMessage, OutboxWork

__all__ = [
    "AuditEvent",
    "Base",
    "InboundMessage",
    "OutboxWork",
    "alembic_owned_tables",
    "get_db",
    "get_engine",
    "get_session_factory",
    "map_to_audit_event",
]
