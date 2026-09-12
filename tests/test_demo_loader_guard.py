"""The demo loader must not be able to destroy a database it does not own.

`demo/load_demo_company.py` runs `01_schema.sql`, which opens with
`DROP TABLE IF EXISTS facturas` and four more. So the guard in front of it is
not a convenience — it is the only thing between a mistyped
`DEMO_DATABASE_URL` and a real company's invoices.

The guard this file specifies identifies the target by a marker the loader
itself wrote, never by the shape of what it finds. Recognising table NAMES
cannot work here: this demo is deliberately named like a real Argentine
distributor's ERP (`facturas`, `factura_lineas`, `articulos`), so the
databases most likely to be hit by accident are exactly the ones whose
contents look familiar.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    """Import `demo/load_demo_company.py`, which is a script, not a package."""
    path = _REPO_ROOT / "demo" / "load_demo_company.py"
    spec = importlib.util.spec_from_file_location("demo_load_demo_company", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_loader = _load_module()
classify_target = _loader.classify_target
DEMO_MARKER_TABLE = _loader.DEMO_MARKER_TABLE


# --- Safe: the two cases where dropping tables destroys nothing of value -----


def test_an_empty_database_is_a_safe_target() -> None:
    """A virgin scratch database is the documented first-load case."""
    assert classify_target(tables=set(), views=set()) is None


def test_a_database_carrying_the_demo_marker_is_a_safe_target() -> None:
    """The marker is the loader's own signature, so a reload is safe.

    Reloading is how a developer gets back to a known state, so this must stay
    allowed — the seed is deterministic and a reload is idempotent.
    """
    reason = classify_target(
        tables={DEMO_MARKER_TABLE, "facturas", "factura_lineas", "articulos"},
        views={"agentsys_sales"},
    )

    assert reason is None


# --- Refused: everything else, including what the old check waved through ----


def test_a_database_shaped_like_the_demo_but_unmarked_is_refused() -> None:
    """THE regression this file exists for.

    The previous check subtracted a set of known demo names from what it found
    and accepted an empty remainder. A real distributor's ERP holding
    `facturas` and `articulos` therefore passed it — and `01_schema.sql` would
    have dropped both. The names are evidence of nothing; only the marker is.
    """
    reason = classify_target(tables={"facturas", "articulos"}, views=set())

    assert reason is not None
    assert "facturas" in reason


def test_a_database_holding_unrelated_tables_is_refused() -> None:
    reason = classify_target(tables={"usuarios", "pagos"}, views=set())

    assert reason is not None


def test_a_database_holding_only_views_is_refused() -> None:
    """Views alone still mean somebody else is using this database.

    The old check asked only for BASE TABLEs, so a database whose tables live
    in another schema and whose `public` holds just views read as empty.
    """
    reason = classify_target(tables=set(), views={"v_ventas"})

    assert reason is not None


@pytest.mark.parametrize(
    "tables",
    [
        pytest.param({"facturas"}, id="one-demo-shaped-table"),
        pytest.param({"padron_clientes", "existencias"}, id="demo-shaped-subset"),
        pytest.param({"facturas", "clientes_reales"}, id="demo-shaped-plus-foreign"),
    ],
)
def test_no_arrangement_of_unmarked_tables_is_ever_accepted(tables: set[str]) -> None:
    """There is no table-name combination that substitutes for the marker."""
    assert classify_target(tables=tables, views=set()) is not None


def test_the_refusal_names_what_it_found_so_the_operator_can_tell_which_db() -> None:
    """An operator who pointed at the wrong database needs to recognise it.

    "refusing to load" with no detail invites the reflex of overriding the
    guard; naming the tables it found makes the mistake obvious instead.
    """
    reason = classify_target(tables={"nomina", "sueldos"}, views=set())

    assert reason is not None
    assert "nomina" in reason
    assert "sueldos" in reason
