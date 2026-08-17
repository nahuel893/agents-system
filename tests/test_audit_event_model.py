"""Tests for the audit_event SQLAlchemy model and schema."""

import uuid
from datetime import datetime, timezone


class TestAuditEventModelImports:
    """T-01: RED — import must fail until model exists."""

    def test_audit_event_model_imports(self) -> None:
        """AuditEvent must be importable from models package."""
        from agentsys.models.audit_event import AuditEvent

        assert AuditEvent is not None

    def test_audit_event_tablename(self) -> None:
        """Table name must be 'audit_event'."""
        from agentsys.models.audit_event import AuditEvent

        assert AuditEvent.__tablename__ == "audit_event"

    def test_audit_event_columns_exist(self) -> None:
        """All required columns are defined on the model."""
        from agentsys.models.audit_event import AuditEvent

        table = AuditEvent.__table__
        column_names = {c.name for c in table.columns}

        required = {
            "id",
            "event_id",
            "occurred_at",
            "correlation_id",
            "sequence",
            "event_type",
            "role",
            "deployment",
            "actor",
            "tool_name",
            "policy_decision",
            "policy_reason",
            "payload",
            "pii_keys",
        }
        assert required.issubset(column_names), (
            f"Missing columns: {required - column_names}"
        )

    def test_audit_event_column_types(self) -> None:
        """Column types match REQ-AUDIT-41."""
        from agentsys.models.audit_event import AuditEvent

        table = AuditEvent.__table__

        # UUID column (SQLAlchemy 2.0 uses 'Uuid' not 'UUID')
        uuid_type_name = table.c.event_id.type.__class__.__name__
        assert "uuid" in uuid_type_name.lower(), (
            f"event_id column should be UUID/Uuid, got {uuid_type_name}"
        )

        # occurred_at is TIMESTAMPTZ (DateTime with timezone)
        assert table.c.occurred_at.type.__class__.__name__ in (
            "DateTime",
            "TIMESTAMP",
        )

        # payload is JSONB
        payload_type_name = table.c.payload.type.__class__.__name__
        assert "JSON" in payload_type_name or "JSONB" in payload_type_name, (
            f"payload column should be JSON/JSONB, got {payload_type_name}"
        )

        # pii_keys is ARRAY (postgresql ARRAY type)
        pii_keys_type_name = table.c.pii_keys.type.__class__.__name__
        assert "ARRAY" in pii_keys_type_name or "List" in pii_keys_type_name, (
            f"pii_keys column should be ARRAY/List, got {pii_keys_type_name}"
        )

    def test_audit_event_unique_constraint(self) -> None:
        """UNIQUE (occurred_at, correlation_id, sequence) is defined.

        PostgreSQL requires partitioned-table UK constraints to include
        all partition key columns. The spec (REQ-AUDIT-41) calls for
        UNIQUE(correlation_id, sequence), which is satisfied by the
        PostgreSQL-compatible composite form.
        """
        from agentsys.models.audit_event import AuditEvent

        table = AuditEvent.__table__
        # Check that the unique constraint exists and covers correlation_id + sequence
        has_unique = any(
            {"correlation_id", "sequence", "occurred_at"} == {col.name for col in c.columns}
            for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        )
        assert has_unique, (
            "audit_event must have UNIQUE (occurred_at, correlation_id, sequence) constraint"
        )

    def test_audit_event_indexes(self) -> None:
        """Required indexes are defined: role+occurred_at, tool_name+occurred_at, GIN payload."""
        from agentsys.models.audit_event import AuditEvent

        table = AuditEvent.__table__

        # BTree index on (role, occurred_at DESC)
        role_occurred_idx = any(
            {"role", "occurred_at"} == {col.name for col in idx.columns}
            for idx in table.indexes
        )
        assert role_occurred_idx, "Missing btree index on (role, occurred_at)"

        # BTree index on (tool_name, occurred_at DESC) — partial
        tool_occurred_idx = any(
            {"tool_name", "occurred_at"} == {col.name for col in idx.columns}
            for idx in table.indexes
        )
        assert tool_occurred_idx, "Missing btree index on (tool_name, occurred_at)"

        # GIN index on payload
        gin_idx = any(
            "gin" in idx.postgresql_using.lower()
            if hasattr(idx, "postgresql_using")
            else "gin" in (idx.name or "").lower()
            for idx in table.indexes
        )
        assert gin_idx, "Missing GIN index on payload"


class TestMapToAuditEventMapper:
    """T-05: map_to_audit_event mapper — converts Pydantic event to ORM row."""

    def test_mapper_function_exists(self) -> None:
        """map_to_audit_event function must be importable."""
        from agentsys.models.audit_event import map_to_audit_event

        assert map_to_audit_event is not None

    def test_mapper_returns_orm_instance(self) -> None:
        """map_to_audit_event returns an AuditEvent ORM instance."""
        from agentsys.models.audit_event import AuditEvent, map_to_audit_event

        event_data = {
            "event_id": uuid.uuid4(),
            "occurred_at": datetime.now(timezone.utc),
            "correlation_id": "test-correlation-123",
            "sequence": 1,
            "event_type": "tool_call_attempted",
            "role": "sales-agent",
            "deployment": "prod",
            "actor": None,
            "tool_name": "order_writer",
            "policy_decision": "allowed",
            "policy_reason": None,
            "payload": {"tool_input": {"phone": "+5491112345678"}},
            "pii_keys": ["tool_input"],
        }

        orm_row = map_to_audit_event(event_data)

        assert isinstance(orm_row, AuditEvent)
        assert orm_row.correlation_id == "test-correlation-123"
        assert orm_row.sequence == 1
        assert orm_row.tool_name == "order_writer"
        assert orm_row.policy_decision == "allowed"
