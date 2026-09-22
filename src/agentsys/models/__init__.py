"""Database models — re-export for convenience.

Importing this package registers EVERY table in ``Base.metadata``. That is a
requirement, not a convenience: while ``audit_event`` was reachable only by
importing ``agentsys.models.audit_event`` directly, the metadata held one
table or two depending on whether some earlier module happened to import it.
Test assertions about the table inventory then passed alone and failed in a
full run, and ``create_all`` raised or not for the same reason.

``audit_event`` is currently the platform's only ORM-declared table (#70
removed the client-owned ``clients``/``orders``/``order_items``/
``conversation_logs``/``catalog_embeddings`` tables and the models submodule
that declared them). The invariant above still holds for whatever the
platform itself owns; a deployment that adds its own tables must import them
the same way to keep them in ``Base.metadata``.
"""

from agentsys.models.audit_event import AuditEvent, map_to_audit_event
from agentsys.models.base import (
    Base,
    alembic_owned_tables,
    get_db,
    get_engine,
    get_session_factory,
)

__all__ = [
    "AuditEvent",
    "Base",
    "alembic_owned_tables",
    "get_db",
    "get_engine",
    "get_session_factory",
    "map_to_audit_event",
]
