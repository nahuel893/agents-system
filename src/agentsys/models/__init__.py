"""Database models — re-export for convenience.

Importing this package registers EVERY table in ``Base.metadata``. That is a
requirement, not a convenience: while ``audit_event`` was reachable only by
importing ``agentsys.models.audit_event`` directly, the metadata held five
tables or six depending on whether some earlier module happened to import it.
Test assertions about the table inventory then passed alone and failed in a
full run, and ``create_all`` raised or not for the same reason.
"""

from agentsys.models.audit_event import AuditEvent, map_to_audit_event
from agentsys.models.base import (
    Base,
    alembic_owned_tables,
    create_orm_owned_tables,
    get_db,
    get_engine,
    get_session_factory,
)
from agentsys.models.tables import (
    CatalogEmbedding,
    Client,
    ConversationLog,
    Order,
    OrderItem,
)

__all__ = [
    "AuditEvent",
    "Base",
    "CatalogEmbedding",
    "Client",
    "ConversationLog",
    "Order",
    "OrderItem",
    "alembic_owned_tables",
    "create_orm_owned_tables",
    "get_db",
    "get_engine",
    "get_session_factory",
    "map_to_audit_event",
]
