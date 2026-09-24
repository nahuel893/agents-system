"""The `agentsys_* -> agents_system_*` view rename migration stays honest.

Issue #45: a deployment that created its four reporting views under the old
`agentsys_*` names before the package rename needs
`demo/migrations/0001_rename_agentsys_to_agents_system_views.sql` to keep
using them. These are static checks -- they run in the default suite with no
database -- so a drift between the migration and the live contract
(`sales_reports.CONTRACT_VIEWS`) is caught immediately rather than only when
someone runs the script by hand. The script is also exercised against a real
Postgres, renamed views and all, in
`tests/test_view_rename_migration_integration.py` (the `demo-reports` CI job).
"""

from __future__ import annotations

import re
from pathlib import Path

from agents_system.connectors.sales_reports import CONTRACT_VIEWS

_MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "demo"
    / "migrations"
    / "0001_rename_agentsys_to_agents_system_views.sql"
).read_text()

_RENAME_RE = re.compile(
    r"ALTER VIEW\s+IF EXISTS\s+(\w+)\s+RENAME TO\s+(\w+)\s*;", re.IGNORECASE
)


def _renames() -> dict[str, str]:
    """old view name -> new view name, as the script actually renames them."""
    return {old: new for old, new in _RENAME_RE.findall(_MIGRATION_SQL)}


def test_the_migration_renames_exactly_the_current_contract_views() -> None:
    """Every `CONTRACT_VIEWS` name must be a rename target, and no other."""
    renames = _renames()

    assert set(renames.values()) == set(CONTRACT_VIEWS)


def test_every_rename_source_is_the_new_name_with_the_old_prefix() -> None:
    """`agents_system_sales` came from `agentsys_sales`, not some other name.

    Written independently of the script's own `agentsys_` <-> `agents_system_`
    substitution, so this cannot pass by construction.
    """
    renames = _renames()

    for old, new in renames.items():
        assert old.startswith("agentsys_")
        assert new == "agents_system_" + old.removeprefix("agentsys_")


def test_the_migration_is_wrapped_in_one_transaction() -> None:
    """A deployment applying this must get all four renames or none."""
    assert re.search(r"^\s*BEGIN\s*;", _MIGRATION_SQL, re.MULTILINE)
    assert re.search(r"^\s*COMMIT\s*;", _MIGRATION_SQL, re.MULTILINE)


def test_every_rename_guards_against_a_missing_source_view() -> None:
    """`IF EXISTS` is what makes a second run of the script a no-op.

    Without it, running the migration again after it already succeeded --
    the documented, expected case for an idempotent script -- would fail with
    "relation \"agentsys_sales\" does not exist" instead of doing nothing.
    """
    for statement in re.findall(r"ALTER VIEW[^;]+;", _MIGRATION_SQL, re.IGNORECASE):
        assert "IF EXISTS" in statement.upper()
