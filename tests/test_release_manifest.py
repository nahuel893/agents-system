"""release-please tracks the released version in two files that must never
disagree: `pyproject.toml`'s `[project].version` (what `pip`/`hatchling`
actually ship) and `.release-please-manifest.json`'s `"."` entry (what
release-please believes is already released, per docs/operations/
release-process.md). A hand-edit to only one of them would make
release-please compute the wrong next version, or ship a wheel whose
metadata disagrees with its own tag -- this test exists so that divergence
fails CI immediately instead of surfacing later as a wrong build (issue
#18).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    version = data["project"]["version"]
    assert isinstance(version, str)
    return version


def _manifest_version() -> str:
    data = json.loads((_REPO_ROOT / ".release-please-manifest.json").read_text())
    version = data["."]
    assert isinstance(version, str)
    return version


def test_pyproject_and_manifest_versions_match() -> None:
    pyproject_version = _pyproject_version()
    manifest_version = _manifest_version()
    assert pyproject_version == manifest_version, (
        f"pyproject.toml version ({pyproject_version!r}) and "
        f".release-please-manifest.json version ({manifest_version!r}) have "
        "diverged. release-please owns both files together -- edit them by "
        "merging its release PR, not by hand."
    )


def test_release_please_config_targets_this_package() -> None:
    config = json.loads((_REPO_ROOT / "release-please-config.json").read_text())
    package = config["packages"]["."]
    assert package["release-type"] == "python"
    assert package["package-name"] == "agents-system"
