"""Tests for agentsys.agent.reasoning — reasoning-block sanitization (spec R5)."""

from typing import Any

import pytest

from agentsys.agent.reasoning import strip_reasoning

# ---------------------------------------------------------------------------
# strip_reasoning — the behavior table (str content)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Well-formed block is removed, surrounding whitespace trimmed.
        ("<think>some reasoning</think>\n\nPONG", "PONG"),
        # Leading whitespace/newline must not defeat the anchor.
        ("\n  <think>some reasoning</think>PONG", "PONG"),
        # No block at all — passthrough, byte-for-byte.
        ("PONG", "PONG"),
        ("", ""),
        # Truncated mid-thought (finish_reason == "length"): the opening tag is
        # never closed, so the whole content is reasoning. Empty beats leaking.
        ("<think>truncated mid thou", ""),
        # NOT anchored — a legitimate answer that merely mentions the tag must
        # survive untouched. This is why the strip is anchored, not global.
        ("Here is how <think> tags work", "Here is how <think> tags work"),
    ],
)
def test_strip_reasoning_behavior_table(raw: str, expected: str) -> None:
    assert strip_reasoning(raw) == expected


def test_strip_reasoning_removes_repeated_leading_blocks() -> None:
    """Consecutive leading blocks are all removed, not just the first."""
    assert strip_reasoning("<think>a</think>\n<think>b</think>\n\nPONG") == "PONG"


def test_strip_reasoning_removes_nested_blocks() -> None:
    """A block that quotes a full <think>...</think> pair must not end early.

    Matching the FIRST closing tag terminates on the INNER one and leaks the
    tail of the real reasoning into the answer. Closing depth has to be
    counted.
    """
    raw = "<think><think>inner</think>still reasoning</think>\n\nPONG"
    assert strip_reasoning(raw) == "PONG"


def test_strip_reasoning_nesting_fix_does_not_swallow_a_later_closing_tag() -> None:
    """The guard against over-correcting the nesting case.

    Simply matching the LAST closing tag would also handle nesting — and would
    eat this legitimate answer whole. Depth counting must stop at the tag that
    actually closes the leading block.
    """
    raw = "<think>r</think>\n\nThe closing tag is </think>"
    assert strip_reasoning(raw) == "The closing tag is </think>"


def test_strip_reasoning_handles_an_empty_block() -> None:
    assert strip_reasoning("<think></think>\n\nPONG") == "PONG"
    assert strip_reasoning("<think></think>") == ""


def test_strip_reasoning_anchor_survives_zero_width_whitespace() -> None:
    """A zero-width space is invisible to str.lstrip() and would bypass the
    anchor entirely, letting a whole reasoning block through untouched."""
    assert strip_reasoning("​<think>r</think>\n\nPONG") == "PONG"
    assert strip_reasoning("﻿ <think>r</think>PONG") == "PONG"


@pytest.mark.parametrize(
    "raw",
    [
        # Uppercase / mixed-case tags — never observed from MiniMax.
        "<THINK>r</THINK>\n\nPONG",
        # Attributed tags — never observed either.
        '<think type="x">r</think>\n\nPONG',
        # A closing tag with no opener is not a block.
        "</think>\n\nPONG",
    ],
)
def test_strip_reasoning_documented_limitations_pass_through(raw: str) -> None:
    """These shapes are NOT handled, deliberately.

    Only the exact lowercase, bare ``<think>`` convention is recognised.
    Asserting the current behavior keeps the limitation intentional and
    visible rather than an accident someone discovers in production.
    """
    assert strip_reasoning(raw) == raw


def test_strip_reasoning_keeps_a_later_tag_mention_after_stripping() -> None:
    """Only the leading block goes; a mention further in the answer stays."""
    raw = "<think>r</think>\n\nA <think> tag looks like this"
    assert strip_reasoning(raw) == "A <think> tag looks like this"


def test_strip_reasoning_handles_real_minimax_shape() -> None:
    """The exact shape observed live against MiniMax-M2.7."""
    raw = (
        "<think>\nThe user asks: 'Reply with exactly one word: PONG'. "
        "So the answer must be exactly 'PONG'.\n</think>\n\nPONG"
    )
    assert strip_reasoning(raw) == "PONG"


# ---------------------------------------------------------------------------
# strip_reasoning — list-of-blocks content (no reasoning may survive in ANY block)
# ---------------------------------------------------------------------------


def test_strip_reasoning_list_strips_every_text_block() -> None:
    """The invariant is per-payload, not per-first-block."""
    content: list[Any] = [
        {"type": "text", "text": "<think>first</think>\n\nHello"},
        {"type": "text", "text": "<think>second</think>\n\nWorld"},
    ]
    assert strip_reasoning(content) == [
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": "World"},
    ]


def test_strip_reasoning_list_leaves_non_text_blocks_untouched() -> None:
    image: dict[str, Any] = {
        "type": "image_url",
        "image_url": {"url": "https://example.test/a.png"},
    }
    content: list[Any] = [{"type": "text", "text": "<think>r</think>\n\nHi"}, image]
    result = strip_reasoning(content)
    assert result == [{"type": "text", "text": "Hi"}, image]


def test_strip_reasoning_list_handles_plain_string_items() -> None:
    assert strip_reasoning(["<think>r</think>\n\nHi", "plain"]) == ["Hi", "plain"]


def test_strip_reasoning_list_never_raises_on_odd_shapes() -> None:
    """Defensive: unexpected block shapes pass through instead of exploding."""
    content: list[Any] = [{"no_text_key": 1}, 42, None]
    assert strip_reasoning(content) == content


def test_strip_reasoning_does_not_mutate_its_input() -> None:
    """The caller's message must be left intact — we rebuild, never mutate."""
    block = {"type": "text", "text": "<think>r</think>\n\nHi"}
    strip_reasoning([block])
    assert block["text"] == "<think>r</think>\n\nHi"
