"""Regression checks for the distributable platform boundary."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

_PLATFORM_ANCHORS = {
    "agentsys/__init__.py",
    "agentsys/connectors/sales_reports.py",
    "agentsys/harness/loader.py",
    "agentsys/integration/meta_signature.py",
    "agentsys/integration/webhook.py",
    "agentsys/integration/whatsapp_client.py",
    "agentsys/platform/roles/base/manifest.md",
}
_DELETED_CLIENT_PATHS = {
    "agentsys/connectors/acme_reports.py",
    "agentsys/connectors/stubs.py",
    "agentsys/models/tables.py",
    "agentsys/services/catalog.py",
    "agentsys/services/clients.py",
    "agentsys/services/conversation_log.py",
    "agentsys/services/seed_data.py",
    "agentsys/services/sync_articles.py",
    "agentsys/services/sync_clients.py",
}


def test_wheel_contains_only_platform_owned_modules(tmp_path: Path) -> None:
    """The published wheel ships platform seams, not client integrations."""
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=project_root,
        check=True,
    )

    wheel = next(tmp_path.glob("agentsys-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

    assert _PLATFORM_ANCHORS <= members
    assert not _DELETED_CLIENT_PATHS & members
    assert not any("acme" in member.casefold() for member in members)
