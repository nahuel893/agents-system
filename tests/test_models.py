"""Tests for ORM model definitions (no running database required)."""

from agentsys.models import (
    Base,
    CatalogEmbedding,
    Client,
    ConversationLog,
    Order,
    OrderItem,
)


def test_all_models_importable() -> None:
    """All five tables are registered in metadata."""
    assert len(Base.metadata.tables) == 5


def test_table_names() -> None:
    """Table names match the PRD schema."""
    expected = {
        "clients",
        "orders",
        "order_items",
        "conversation_logs",
        "catalog_embeddings",
    }
    assert set(Base.metadata.tables.keys()) == expected


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
