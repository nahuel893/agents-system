"""Docs must not reference the pre-archive library-first-agents path.

Review finding on PR #114: the ADR-004 archive pass moved
`openspec/changes/library-first-agents/` to
`openspec/changes/archive/2026-09-26-library-first-agents/` (via `git mv`),
but four EN/ES doc pairs still pointed at the old, now-nonexistent path:
`docs/delivery/live-test-plan.md` (and its `_es` twin) and
`docs/architecture/adr-007-read-only-sql-tool.md` (and its `_es` twin).

`docs/delivery/live-test-plan.md`'s Phase 4 section also described the
library-first-agents dependency as still blocking, which stopped being true
once all ten ADR-004 PR slices merged and the change archived.

This is a regression gate: it fails on any future stale reference to the
pre-archive path, not just the ones fixed here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

STALE_PATH = "openspec/changes/library-first-agents/"

DOC_FILES = [
    REPO_ROOT / "docs/delivery/live-test-plan.md",
    REPO_ROOT / "docs/delivery_es/live-test-plan.md",
    REPO_ROOT / "docs/architecture/adr-007-read-only-sql-tool.md",
    REPO_ROOT / "docs/architecture_es/adr-007-read-only-sql-tool.md",
]


def test_docs_do_not_reference_the_pre_archive_path() -> None:
    offenders: list[str] = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if STALE_PATH in line:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "Stale reference(s) to the pre-archive library-first-agents path "
        "(openspec/changes/library-first-agents/ no longer exists on disk; "
        "it was archived to "
        "openspec/changes/archive/2026-09-26-library-first-agents/):\n"
        + "\n".join(offenders)
    )


def test_live_test_plan_phase_4_no_longer_reads_as_blocked() -> None:
    """All ten ADR-004 PR slices merged and the change archived (see
    openspec/changes/archive/2026-09-26-library-first-agents/archive-report.md),
    so Phase 4 is unblocked; the doc must not still claim it is blocked or
    that the dependency has not merged.
    """
    for path in (
        REPO_ROOT / "docs/delivery/live-test-plan.md",
        REPO_ROOT / "docs/delivery_es/live-test-plan.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert "blocked by ADR-004" not in text, path
        assert "bloqueada por ADR-004" not in text, path
        assert "not started until that work merges" not in text, path
        assert "no comienza hasta que ese\ntrabajo se fusione" not in text, path
