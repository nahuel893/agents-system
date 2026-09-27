"""release-please tracks the released version in three files that must never
disagree: `pyproject.toml`'s `[project].version` (what `pip`/`hatchling`
actually ship), `.release-please-manifest.json`'s `"."` entry (what
release-please believes is already released), and `uv.lock`'s own
`agents-system` package entry (what `uv` resolves the local project to),
per docs/operations/release-process.md. A hand-edit to only one of them
would make release-please compute the wrong next version, ship a wheel
whose metadata disagrees with its own tag, or leave `uv.lock` pointing at a
stale self-version (issue #18, issue #150) -- this test exists so that
divergence fails CI immediately instead of surfacing later as a wrong
build.
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


def _uv_lock_version() -> str:
    data = tomllib.loads((_REPO_ROOT / "uv.lock").read_text())
    packages = [
        package for package in data["package"] if package["name"] == "agents-system"
    ]
    assert len(packages) == 1, (
        "expected exactly one 'agents-system' entry in uv.lock's [[package]] "
        f"array, found {len(packages)}"
    )
    version = packages[0]["version"]
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


def test_pyproject_and_uv_lock_versions_match() -> None:
    pyproject_version = _pyproject_version()
    uv_lock_version = _uv_lock_version()
    assert pyproject_version == uv_lock_version, (
        f"pyproject.toml version ({pyproject_version!r}) and uv.lock's own "
        f"agents-system entry ({uv_lock_version!r}) have diverged. Run "
        "`uv lock` to resync uv.lock after a manual version edit -- a "
        "release cut with `scripts/release.sh pr` should not hit this, "
        "since release-please's `extra-files` rule bumps uv.lock's entry "
        "in the same commit as pyproject.toml (see issue #150)."
    )
