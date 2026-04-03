"""Database models — re-export for convenience."""

from badie.models.base import Base, get_db, get_engine, get_session_factory
from badie.models.tables import (
    CatalogEmbedding,
    Client,
    ConversationLog,
    Order,
    OrderItem,
)

__all__ = [
    "Base",
    "CatalogEmbedding",
    "Client",
    "ConversationLog",
    "Order",
    "OrderItem",
    "get_db",
    "get_engine",
    "get_session_factory",
]
