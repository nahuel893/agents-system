"""Tests for ORM model definitions (no running database required)."""

from agentsys.models import (
    Base,
    CatalogEmbedding,
    Client,
    ConversationLog,
    Order,
    OrderItem,
)

# Imported for its side effect: registering audit_event in Base.metadata.
# Without this import the inventory assertions below depend on whether some
# other test module imported the audit model first — they passed in isolation
# and failed in a full run.
from agentsys.models.audit_event import AuditEvent  # noqa: F401

EXPECTED_TABLES = {
    "clients",
    "orders",
    "order_items",
    "conversation_logs",
    "catalog_embeddings",
    "audit_event",
}


#: Every table the ORM declares. Importing ``agentsys.models`` must register
#: all of them, so this inventory is the same whether the suite runs whole or
#: one module at a time — it did not used to be.
EXPECTED_TABLES = {
    "clients",
    "orders",
    "order_items",
    "conversation_logs",
    "catalog_embeddings",
    "audit_event",
}


def test_all_models_importable() -> None:
    """Every declared table is registered in metadata, and nothing else is."""
    assert len(Base.metadata.tables) == len(EXPECTED_TABLES)


def test_table_names() -> None:
    """Table names match the PRD schema plus the D-007 audit log."""
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


def test_catalog_embedding_vector_dimension() -> None:
    """Embedding column uses 512 dimensions (Matryoshka)."""
    col = CatalogEmbedding.__table__.c.embedding
    assert col.type.dim == 512


def test_client_relationships() -> None:
    """Client model defines orders and conversation_logs relationships."""
    rel_names = {r.key for r in Client.__mapper__.relationships}
    assert "orders" in rel_names
    assert "conversation_logs" in rel_names


def test_order_relationships() -> None:
    """Order model defines client and items relationships."""
    rel_names = {r.key for r in Order.__mapper__.relationships}
    assert "client" in rel_names
    assert "items" in rel_names


def test_order_item_relationship() -> None:
    """OrderItem model links back to order."""
    rel_names = {r.key for r in OrderItem.__mapper__.relationships}
    assert "order" in rel_names


def test_conversation_log_relationship() -> None:
    """ConversationLog links back to client."""
    rel_names = {r.key for r in ConversationLog.__mapper__.relationships}
    assert "client" in rel_names


def test_clients_phone_number_index() -> None:
    """clients table has an index on phone_number."""
    index_names = {idx.name for idx in Client.__table__.indexes}
    assert "ix_clients_phone_number" in index_names


def test_clients_external_id_unique() -> None:
    """clients.external_id is a nullable, unique Integer column for medallion sync."""
    col = Client.__table__.c.external_id
    assert col.nullable is True
    assert col.unique is True
