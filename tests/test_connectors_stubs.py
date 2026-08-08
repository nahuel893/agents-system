"""Tests for BADIE sales-agent connector stubs (D-006).

Stubs are deterministic fake connectors that return realistic data shapes.
They allow end-to-end testing of the harness pipeline without real external
dependencies (WhatsApp API, Postgres, etc.).

Strict TDD: tests written before stubs.py exists.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# catalog_search
# ---------------------------------------------------------------------------

def test_catalog_search_returns_results_list() -> None:
    from agentsys.connectors.stubs import catalog_search

    result = catalog_search({"q": "azucar"})

    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) >= 1


def test_catalog_search_result_shape() -> None:
    from agentsys.connectors.stubs import catalog_search

    item = catalog_search({"q": "azucar"})["results"][0]

    assert "id" in item
    assert "name" in item
    assert "price" in item
    assert "stock" in item
    assert isinstance(item["price"], float)
    assert isinstance(item["stock"], int)


def test_catalog_search_empty_query_returns_catalog() -> None:
    from agentsys.connectors.stubs import catalog_search

    result = catalog_search({"q": ""})

    assert len(result["results"]) >= 1


# ---------------------------------------------------------------------------
# client_lookup
# ---------------------------------------------------------------------------

def test_client_lookup_known_phone_returns_client() -> None:
    from agentsys.connectors.stubs import client_lookup

    result = client_lookup({"phone": "5491112345678"})

    assert result["client_id"] is not None
    assert "name" in result
    assert result["phone"] == "5491112345678"


def test_client_lookup_unknown_phone_returns_none() -> None:
    from agentsys.connectors.stubs import client_lookup

    result = client_lookup({"phone": "0000000000"})

    assert result["client_id"] is None


# ---------------------------------------------------------------------------
# order_writer
# ---------------------------------------------------------------------------

def test_order_writer_returns_order_id_and_status() -> None:
    from agentsys.connectors.stubs import order_writer

    result = order_writer({
        "client_id": "cl-001",
        "items": [{"product_id": "prod-001", "qty": 2}],
    })

    assert "order_id" in result
    assert result["status"] == "created"
    assert isinstance(result["total"], float)


def test_order_writer_total_reflects_items() -> None:
    from agentsys.connectors.stubs import order_writer

    result = order_writer({
        "client_id": "cl-001",
        "items": [
            {"product_id": "prod-001", "qty": 2},
            {"product_id": "prod-002", "qty": 1},
        ],
    })

    assert result["total"] > 0.0


# ---------------------------------------------------------------------------
# message_sender
# ---------------------------------------------------------------------------

def test_message_sender_returns_sent_status() -> None:
    from agentsys.connectors.stubs import message_sender

    result = message_sender({"to": "5491112345678", "text": "Hola, ¿en qué te puedo ayudar?"})

    assert result["status"] == "sent"
    assert "message_id" in result


# ---------------------------------------------------------------------------
# session_state
# ---------------------------------------------------------------------------

def test_session_state_get_returns_data() -> None:
    from agentsys.connectors.stubs import session_state

    result = session_state({"action": "get", "session_id": "s-001"})

    assert "session_id" in result
    assert "data" in result


def test_session_state_set_returns_ok() -> None:
    from agentsys.connectors.stubs import session_state

    result = session_state({"action": "set", "session_id": "s-001", "data": {"step": "greeting"}})

    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# build_badie_registry — end-to-end wiring
# ---------------------------------------------------------------------------

def test_build_badie_registry_has_exactly_the_expected_tools() -> None:
    """Exact set, on purpose: the registry is the platform's promise about what
    it can equip, so silent growth is as much a defect as a missing tool.

    `run_report` is here unbound (no BI engine) rather than absent.
    `platform/roles/data-agent` names it, and a tool a manifest names but the
    registry lacks makes the whole role unbuildable through InjectionError —
    not partially usable. Calling it without BI_DATABASE_URL returns a
    "not configured" result; main.py swaps in the real read-only engine.
    """
    from agentsys.connectors.stubs import build_badie_registry

    registry = build_badie_registry()

    assert set(registry.names()) == {
        "catalog_search",
        "client_lookup",
        "order_writer",
        "message_sender",
        "session_state",
        "knowledge_retrieval",
        "conversation_summarizer",
        "escalation_notifier",
        "run_report",
    }


def test_build_badie_registry_connectors_are_callable() -> None:
    from agentsys.connectors.stubs import build_badie_registry

    registry = build_badie_registry()

    spec = registry.get("catalog_search")
    result = spec.connector({"q": "harina"})
    assert "results" in result
