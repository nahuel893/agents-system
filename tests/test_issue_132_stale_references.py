"""Docs/examples left stale by recent PRs, found during adversarial review
(issue #132). Each test below is a regression gate for one checklist item --
it fails on any future reintroduction of the exact defect, not just today's
instance.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# harness-conventions/SKILL.md -- "When you add a capability" checklist
# ---------------------------------------------------------------------------


def test_harness_conventions_capability_checklist_names_registration_and_grant() -> (
    None
):
    """After ADR-004 4b (PR 98), a runtime is only built when it is
    registered in `AGENT_REGISTRATIONS`/`create_app(agents=...)` AND granted
    in `DEPLOY_GRANTS`. The checklist's step 3 used to say only "Wire it
    into the lifespan runtime cache + `adapter_runtimes` in config" --
    following that step alone now hits a boot failure (no grant, no
    registration). The step must name all three: registration, grant, and
    the id-naming var (`ADAPTER_RUNTIMES`/`WHATSAPP_RUNTIME_ID`).
    """
    text = _read(".claude/skills/harness-conventions/SKILL.md")

    assert (
        "Wire it into the lifespan runtime cache + `adapter_runtimes` in config."
        not in text
    ), "the stale capability-checklist step must be replaced, not just supplemented"
    assert "AGENT_REGISTRATIONS" in text
    assert "DEPLOY_GRANTS" in text
    assert "ADAPTER_RUNTIMES" in text or "WHATSAPP_RUNTIME_ID" in text


# ---------------------------------------------------------------------------
# docs/delivery/live-test-plan.md (+ ES twin) -- Gating heading and #81 row
# ---------------------------------------------------------------------------


def test_live_test_plan_gating_heading_no_longer_reads_to_build() -> None:
    """Issue #81/PR 100 built live gating; the heading still read "(to
    build)" (and the ES twin "(a construir)") after that merged."""
    en = _read("docs/delivery/live-test-plan.md")
    es = _read("docs/delivery_es/live-test-plan.md")

    assert "## Gating (to build)" not in en
    assert "## Gating (a construir)" not in es


def test_live_test_plan_81_row_is_annotated_like_its_sibling_82_row() -> None:
    """The tracking table's `#82` row carries a "(fixed, ...)" annotation;
    the `#81` row (also long since built) carried none. Both EN and ES rows
    must be annotated now."""
    en = _read("docs/delivery/live-test-plan.md")
    es = _read("docs/delivery_es/live-test-plan.md")

    assert "| Live gating on thresholds |" not in en, (
        "the #81 row must carry a completion annotation, like the sibling #82 row"
    )
    assert "#81 |" in en and "built" in en.split("#81 |")[0].rsplit("\n", 1)[-1], (
        "the #81 row's own line must say it was built"
    )
    assert "| Gating de los tests en vivo con umbrales |" not in es
    assert "construido" in es.split("#81 |")[0].rsplit("\n", 1)[-1]


# ---------------------------------------------------------------------------
# docs/platform/live-eval.md (+ ES twin) -- guardrail completed-ratio floor
# ---------------------------------------------------------------------------


def test_live_eval_doc_describes_the_guardrail_completed_ratio_floor() -> None:
    """`_scenario_gate` (runner.py, PR #100 review fix) also requires at
    least `GUARDRAIL_MIN_COMPLETED_RATIO` (50%) of a guardrail scenario's
    TOTAL runs to have completed before it grades the exercised/held ratio,
    with its own distinct "too few completed runs" failure reason. The
    Gating section (EN + ES) described only "exercised at least once" plus
    "100% held", omitting this floor entirely."""
    en = _read("docs/platform/live-eval.md")
    es = _read("docs/platform_es/live-eval.md")

    assert "GUARDRAIL_MIN_COMPLETED_RATIO" in en
    assert "50%" in en.split("GUARDRAIL_MIN_COMPLETED_RATIO")[1][:400]

    assert "GUARDRAIL_MIN_COMPLETED_RATIO" in es
    assert "50%" in es.split("GUARDRAIL_MIN_COMPLETED_RATIO")[1][:400]
