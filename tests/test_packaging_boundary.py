"""Regression checks for the distributable platform boundary."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

_PLATFORM_ANCHORS = {
    "agents_system/__init__.py",
    "agents_system/connectors/sales_reports.py",
    "agents_system/harness/loader.py",
    "agents_system/integration/meta_signature.py",
    "agents_system/integration/webhook.py",
    "agents_system/integration/whatsapp_client.py",
    "agents_system/platform/roles/base/manifest.md",
}
_DELETED_CLIENT_PATHS = {
    "agents_system/connectors/acme_reports.py",
    "agents_system/connectors/stubs.py",
    "agents_system/models/tables.py",
    "agents_system/services/catalog.py",
    "agents_system/services/clients.py",
    "agents_system/services/conversation_log.py",
    "agents_system/services/seed_data.py",
    "agents_system/services/sync_articles.py",
    "agents_system/services/sync_clients.py",
}


def test_wheel_contains_only_platform_owned_modules(tmp_path: Path) -> None:
    """The published wheel ships platform seams, not client integrations."""
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=project_root,
        check=True,
    )

    wheel = next(tmp_path.glob("agents_system-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

    assert _PLATFORM_ANCHORS <= members
    assert not _DELETED_CLIENT_PATHS & members
    assert not any("acme" in member.casefold() for member in members)
