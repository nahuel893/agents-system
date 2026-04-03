"""ORM models matching the PRD §14.1 data schema."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from badie.models.base import Base


class Client(Base):
    """A client (punto de venta) identified by phone number."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    business_type: Mapped[str | None] = mapped_column(String(100))
    zone: Mapped[str | None] = mapped_column(String(100))
    price_list_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    # Relationships
    orders: Mapped[list["Order"]] = relationship(back_populates="client")
    conversation_logs: Mapped[list["ConversationLog"]] = relationship(
        back_populates="client"
    )


class Order(Base):
    """A purchase order created during a conversation."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clients.id")
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    # Relationships
    client: Mapped["Client | None"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")


class OrderItem(Base):
    """A single line-item within an order."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id")
    )
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    subtotal: Mapped[float | None] = mapped_column(Numeric(12, 2))

    # Relationships
    order: Mapped["Order | None"] = relationship(back_populates="items")


class ConversationLog(Base):
    """A single message in a conversation, for auditing and future fine-tuning."""

    __tablename__ = "conversation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(50), nullable=False)
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clients.id")
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    model_used: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    # Relationships
    client: Mapped["Client | None"] = relationship(back_populates="conversation_logs")


class CatalogEmbedding(Base):
    """Embedding vector for a catalog product (pgvector, Matryoshka 512d)."""

    __tablename__ = "catalog_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(512))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()"
    )

    __table_args__ = (
        Index(
            "ix_catalog_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
