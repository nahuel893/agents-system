"""Tests for ORM model definitions (no running database required)."""

from agentsys.models import Base

# Imported for its side effect: registering audit_event in Base.metadata.
# Without this import the inventory assertions below depend on whether some
# other test module imported the audit model first — they passed in isolation
# and failed in a full run.
from agentsys.models.audit_event import AuditEvent  # noqa: F401

#: Every table the ORM declares. Importing ``agentsys.models`` must register
#: all of them, so this inventory is the same whether the suite runs whole or
#: one module at a time — it did not used to be.
#:
#: The platform owns ``audit_event`` plus the durable webhook inbox/outbox
#: tables. (#70 deleted the client-owned ``clients``/``orders``/``order_items``/
#: ``conversation_logs``/``catalog_embeddings`` tables and the models submodule
#: that declared them.) A deployment that adds its own ORM tables extends this
#: inventory by importing them the same way.
EXPECTED_TABLES = {
    "audit_event",
    "outbox_work",
    "webhook_inbox",
}


def test_all_models_importable() -> None:
    """Every declared table is registered in metadata, and nothing else is."""
    assert len(Base.metadata.tables) == len(EXPECTED_TABLES)


def test_table_names() -> None:
    """Table names match the platform's own schema."""
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES
