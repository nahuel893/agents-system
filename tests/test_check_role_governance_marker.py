"""Unit tests for `scripts/check_role_governance_marker.py`'s
`commit_messages_carry_breaking_marker` (PR6-T2, design.md D8's CI-enforced
half of Guard 1): a pure function over a list of commit messages, in
isolation from any real git range or GitHub Actions event -- this repo has
no existing workflow that exercises a synthetic PR, so a fixture-PR-diff
integration test is not the right shape here.
"""

from __future__ import annotations

from scripts.check_role_governance_marker import commit_messages_carry_breaking_marker


def test_bang_before_colon_in_subject_line_is_a_breaking_marker() -> None:
    messages = [
        "fix(harness): unrelated commit",
        "feat(roles)!: widen sales-agent tools",
    ]
    assert commit_messages_carry_breaking_marker(messages) is True


def test_breaking_change_footer_anywhere_in_the_body_is_a_breaking_marker() -> None:
    messages = [
        "chore: unrelated commit",
        (
            "feat(roles): widen sales-agent tools\n\n"
            "BREAKING CHANGE: sales-agent gained a new tool"
        ),
    ]
    assert commit_messages_carry_breaking_marker(messages) is True


def test_no_marker_anywhere_returns_false() -> None:
    messages = [
        "fix(harness): unrelated commit",
        "feat(roles): widen sales-agent tools, no marker at all",
    ]
    assert commit_messages_carry_breaking_marker(messages) is False


def test_empty_list_returns_false() -> None:
    assert commit_messages_carry_breaking_marker([]) is False


def test_bang_after_the_first_colon_does_not_count() -> None:
    """The `!` must sit immediately before the FIRST `:` (the
    conventional-commits type/scope separator) -- one appearing later, e.g.
    inside the description, is not the breaking-change shorthand."""
    messages = ["feat(roles): sales-agent tools changed! see docs for details"]
    assert commit_messages_carry_breaking_marker(messages) is False


def test_breaking_change_footer_is_case_sensitive_to_the_convention() -> None:
    """`BREAKING CHANGE:` is the exact conventional-commits footer token --
    a lowercase or reworded mention is not the marker."""
    messages = ["feat(roles): widen sales-agent tools\n\nbreaking change: not it"]
    assert commit_messages_carry_breaking_marker(messages) is False
