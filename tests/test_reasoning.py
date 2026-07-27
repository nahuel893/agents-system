"""Tests for agentsys.agent.reasoning — reasoning-block sanitization (spec R5)."""

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentsys.agent.reasoning import ReasoningSanitizedChatOpenAI, strip_reasoning

# ---------------------------------------------------------------------------
# strip_reasoning — the AD-3 behavior table (str content)
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
    """The guard against over-correcting the nesting bug.

    Simply matching the LAST closing tag would also fix nesting — and would
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
    """The exact shape observed live against MiniMax-M2.7 (obs #409)."""
    raw = (
        "<think>\nThe user asks: 'Reply with exactly one word: PONG'. "
        "So the answer must be exactly 'PONG'.\n</think>\n\nPONG"
    )
    assert strip_reasoning(raw) == "PONG"


# ---------------------------------------------------------------------------
# strip_reasoning — list-of-blocks content (spec R5: "no reasoning in ANY block")
# ---------------------------------------------------------------------------


def test_strip_reasoning_list_strips_every_text_block() -> None:
    """Spec R5 requires no reasoning remains in ANY block — not just the first."""
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


# ---------------------------------------------------------------------------
# ReasoningSanitizedChatOpenAI — result rebuilding (spec R4, R5)
# ---------------------------------------------------------------------------


def _model() -> ReasoningSanitizedChatOpenAI:
    return ReasoningSanitizedChatOpenAI(
        model="test-model",
        base_url="https://example.test/v1",
        api_key="test-key",
    )


def test_sanitize_clears_reasoning_from_message_and_generation_text() -> None:
    """``ChatGeneration.text`` is snapshotted at construction and does NOT
    follow mutation of ``message.content``.

    An implementation that mutates the message in place would still leave the
    raw reasoning in ``.text`` for any consumer that reads it. Asserting on
    ``.text`` is what catches that; asserting only on ``message.content``
    would pass against the buggy version.
    """
    msg = AIMessage(content="<think>reasoning</think>\n\nPONG")
    result = ChatResult(generations=[ChatGeneration(message=msg)])

    gen = _model()._sanitize(result).generations[0]

    assert gen.message.content == "PONG"
    assert "<think>" not in gen.text
    assert gen.text == "PONG"


def test_sanitize_does_not_mutate_the_original_message() -> None:
    msg = AIMessage(content="<think>r</think>\n\nPONG")
    _model()._sanitize(ChatResult(generations=[ChatGeneration(message=msg)]))
    assert msg.content == "<think>r</think>\n\nPONG"


def test_sanitize_preserves_tool_calls_and_metadata() -> None:
    """Only content changes; tool calls and metadata survive byte-for-byte.

    This is the shape MiniMax actually returns when calling a tool: the think
    block sits in content while tool_calls is populated alongside it.
    """
    msg = AIMessage(
        content="<think>the user wants a catalog lookup</think>",
        tool_calls=[
            {
                "name": "catalog_search",
                "args": {"query": "Coca Cola 2L"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
        additional_kwargs={"refusal": None},
        response_metadata={"finish_reason": "tool_calls"},
        id="msg-1",
        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    )
    result = ChatResult(
        generations=[
            ChatGeneration(message=msg, generation_info={"finish_reason": "tool_calls"})
        ],
        llm_output={"model_name": "test-model"},
    )

    sanitized = _model()._sanitize(result)
    out = sanitized.generations[0]

    assert out.message.tool_calls == msg.tool_calls
    assert out.message.additional_kwargs == msg.additional_kwargs
    assert out.message.response_metadata == msg.response_metadata
    assert out.message.id == msg.id
    assert out.message.usage_metadata == msg.usage_metadata
    assert out.generation_info == {"finish_reason": "tool_calls"}
    assert sanitized.llm_output == {"model_name": "test-model"}
    # The whole content was reasoning, so nothing user-facing remains.
    assert out.message.content == ""


def test_sanitize_handles_multiple_generations() -> None:
    result = ChatResult(
        generations=[
            ChatGeneration(message=AIMessage(content="<think>a</think>\n\nONE")),
            ChatGeneration(message=AIMessage(content="<think>b</think>\n\nTWO")),
        ]
    )
    out = _model()._sanitize(result)
    assert [g.message.content for g in out.generations] == ["ONE", "TWO"]


def test_sanitize_passes_clean_content_through() -> None:
    result = ChatResult(generations=[ChatGeneration(message=AIMessage(content="PONG"))])
    assert _model()._sanitize(result).generations[0].message.content == "PONG"


async def test_agenerate_sanitizes_the_result() -> None:
    """The async path is what graph.py uses (ainvoke -> _agenerate)."""
    canned = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="<think>r</think>\n\nPONG"))]
    )
    with patch(
        "langchain_openai.chat_models.base.BaseChatOpenAI._agenerate",
        new=AsyncMock(return_value=canned),
    ):
        out = await _model()._agenerate([HumanMessage(content="hi")])

    assert out.generations[0].message.content == "PONG"
    assert "<think>" not in out.generations[0].text


def test_generate_sanitizes_the_result() -> None:
    """The sync path must not be a bypass around sanitization."""
    canned = ChatResult(
        generations=[ChatGeneration(message=AIMessage(content="<think>r</think>\n\nPONG"))]
    )
    with patch(
        "langchain_openai.chat_models.base.BaseChatOpenAI._generate",
        new=Mock(return_value=canned),
    ):
        out = _model()._generate([HumanMessage(content="hi")])

    assert out.generations[0].message.content == "PONG"
    assert "<think>" not in out.generations[0].text


def test_bind_tools_keeps_the_sanitizing_model_on_the_call_path() -> None:
    """graph.py calls .bind_tools() on whatever the factory returns.

    Returning a composed ``model | parser`` would have no bind_tools at all,
    which is why the sanitizer is a subclass. The binding must still wrap our
    class, or sanitization silently stops applying once tools are equipped.
    """
    model = _model()
    bound = model.bind_tools(
        [
            {
                "name": "catalog_search",
                "description": "Search the catalog.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
    )
    assert isinstance(bound.bound, ReasoningSanitizedChatOpenAI)


async def test_bind_tools_still_sanitizes_end_to_end() -> None:
    """The structural isinstance check above is necessary but not sufficient.

    It would keep passing even if the override stopped being reached through
    the binding. This drives a real invocation through the bound object with
    the underlying generate layer patched, so it fails if sanitization ever
    silently stops applying once tools are equipped — which is the only way
    the agent ever calls this model.
    """
    canned = ChatResult(
        generations=[
            ChatGeneration(
                message=AIMessage(
                    content="<think>they want a catalog lookup</think>\n\nChecking that.",
                    tool_calls=[
                        {
                            "name": "catalog_search",
                            "args": {"query": "Coca Cola 2L"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                )
            )
        ]
    )
    bound = _model().bind_tools(
        [
            {
                "name": "catalog_search",
                "description": "Search the catalog.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
    )

    with patch(
        "langchain_openai.chat_models.base.BaseChatOpenAI._agenerate",
        new=AsyncMock(return_value=canned),
    ):
        response = await bound.ainvoke("Do we have Coca Cola 2L?")

    assert response.content == "Checking that."
    assert "<think>" not in str(response.content)
    assert response.tool_calls[0]["name"] == "catalog_search"
    assert response.tool_calls[0]["args"] == {"query": "Coca Cola 2L"}
