#!/usr/bin/env python3
"""Guard 1's CI-enforced half (design.md D8; ``predefined-agent-governance``
spec, "A contract check fails on any tools/permissions divergence lacking
both a version bump and a CHANGELOG entry"): on a PR touching any
``platform/roles/**/manifest.md``, require at least one commit in range to
carry a conventional-commit breaking-change marker -- the marker that
actually produces the ``CHANGELOG.md`` entry release-please writes at the
next release (the pytest contract test in ``tests/platform_role_contract.py``
checks the version-bump/snapshot half; this script checks the commit-marker
half, since a real ``CHANGELOG.md`` entry cannot exist yet at PR time).

Run as ``.venv/bin/python scripts/check_role_governance_marker.py`` from a
CI job with ``fetch-depth: 0`` (the commit range must be locally walkable,
same requirement the ``secret-scan`` job's own comment documents for
``gitleaks-action``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_MANIFEST_GLOB_PREFIX = "platform/roles/"
_MANIFEST_GLOB_SUFFIX = "/manifest.md"
_BREAKING_CHANGE_FOOTER = "BREAKING CHANGE:"


def commit_messages_carry_breaking_marker(messages: list[str]) -> bool:
    """True when at least one message's first line carries conventional
    commits' own `!` breaking-change shorthand (a `!` immediately before the
    first `:`), OR any message contains the literal `BREAKING CHANGE:`
    footer anywhere. False for an empty list or when neither is found."""
    for message in messages:
        subject = message.splitlines()[0] if message else ""
        colon_index = subject.find(":")
        if colon_index > 0 and subject[colon_index - 1] == "!":
            return True
        if _BREAKING_CHANGE_FOOTER in message:
            return True
    return False


def _is_role_manifest_path(path: str) -> bool:
    return path.startswith(_MANIFEST_GLOB_PREFIX) and path.endswith(
        _MANIFEST_GLOB_SUFFIX
    )


def _resolve_base_and_head() -> tuple[str, str]:
    """The same base/head convention `gitleaks-action` uses (per the
    `secret-scan` job's own comment in `.github/workflows/ci.yml`): on
    `pull_request`, `baseRef^..headRef` from the PR's own base/head SHAs; on
    `push`, the commits the push introduced (`before`..`after`) -- both read
    from the event payload GitHub Actions writes to `GITHUB_EVENT_PATH`, the
    same source `gitleaks-action` itself reads (`src/gitleaks.js`'s
    `ScanPullRequest`, `src/index.js`'s push handling)."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event: dict = {}
    if event_path:
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)

    if event_name == "pull_request":
        pull_request = event.get("pull_request", {})
        base = pull_request.get("base", {}).get("sha", "HEAD^")
        head = pull_request.get("head", {}).get("sha", "HEAD")
    else:
        base = event.get("before", "HEAD^")
        head = event.get("after", os.environ.get("GITHUB_SHA", "HEAD"))
    return base, head


def _changed_files(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _commit_messages(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", f"{base}..{head}", "--format=%B%x00"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [chunk for chunk in result.stdout.split("\x00") if chunk.strip()]


def main() -> int:
    base, head = _resolve_base_and_head()
    changed = _changed_files(base, head)
    touched_manifests = [path for path in changed if _is_role_manifest_path(path)]
    if not touched_manifests:
        return 0  # Guard 1 does not apply -- no predefined-role manifest changed.

    messages = _commit_messages(base, head)
    if commit_messages_carry_breaking_marker(messages):
        return 0

    print(
        "Guard 1: this PR changes predefined-role manifest(s) "
        f"{touched_manifests} but no commit in range carries a "
        "conventional-commit `!` marker or a `BREAKING CHANGE:` footer. "
        "A predefined role's tools/permissions surface is a shared safety "
        "baseline -- mark the commit that changes it as breaking.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
