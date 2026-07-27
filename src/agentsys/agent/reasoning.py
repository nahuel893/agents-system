"""Reasoning-block sanitization for OpenAI-compatible reasoning models.

Some models served behind an OpenAI-compatible endpoint return their
chain-of-thought inline in ``message.content``, wrapped in ``<think>...</think>``,
with no separate reasoning field. MiniMax does this on every response and it
cannot be turned off from the request side: ``reasoning_effort: "none"``,
``chat_template_kwargs.enable_thinking: false`` and ``thinking.type: "disabled"``
are all accepted with HTTP 200 and silently ignored.

Left alone, that reasoning text flows straight to the end user, because
``agent/graph.py`` returns the message as-is and the integration layer reads
``content`` directly.

This module holds the pure text half of the fix. The chat-model wrapper that
applies it to real responses arrives separately.

LIMITATIONS, deliberate and covered by tests that assert the current behavior
so they stay intentional rather than becoming surprises: only the exact
lowercase, bare ``<think>`` spelling is recognised. Uppercase (``<THINK>``) and
attributed (``<think type="x">``) variants pass through, as does a stray
``</think>`` with no opener. None of these have been observed from MiniMax;
generalising to other conventions belongs with the wider provider work.
"""

from __future__ import annotations

from typing import Any

_OPEN = "<think>"
_CLOSE = "</think>"

# Characters allowed before the tag without breaking the anchor. Plain
# ``str.strip()`` covers whitespace but NOT zero-width marks — and a single
# invisible zero-width space ahead of the tag would defeat the anchor and let
# an entire reasoning block through untouched.
_INVISIBLE = " \t\n\r\v\f​‌‍⁠﻿"


def _find_block_end(text: str) -> int:
    """Index just past the ``</think>`` that closes the block opening at 0.

    Nesting depth is counted, because a reasoning block that quotes a whole
    ``<think>...</think>`` pair would otherwise terminate on the INNER closing
    tag and leave the tail of the real reasoning sitting in the answer.

    Matching the LAST closing tag would also handle nesting — and would swallow
    a legitimate answer that merely mentions the tag further down. Counting
    depth stops at the tag that actually closes the leading block, which is the
    only option that handles both.

    Returns -1 when the block is never closed.
    """
    depth = 0
    index = 0
    while index < len(text):
        if text.startswith(_OPEN, index):
            depth += 1
            index += len(_OPEN)
        elif text.startswith(_CLOSE, index):
            depth -= 1
            index += len(_CLOSE)
            if depth == 0:
                return index
        else:
            index += 1
    return -1


def _strip_text(text: str) -> str:
    """Remove leading reasoning blocks from one text value.

    The match is ANCHORED to the start of the value. A global search would
    corrupt a legitimate answer that merely mentions the tag — e.g. an agent
    explaining reasoning-model syntax — so anything after the leading block is
    left alone.

    An unterminated opening tag means the response was truncated mid-thought
    (``finish_reason == "length"``); everything from the tag onward is
    reasoning, so the result is empty. Returning nothing beats leaking a
    half-finished thought.
    """
    result = text
    removed = False

    while True:
        candidate = result.lstrip(_INVISIBLE)
        if not candidate.startswith(_OPEN):
            break
        end = _find_block_end(candidate)
        if end == -1:
            return ""
        result = candidate[end:]
        removed = True

    # Only normalise the edges when something was actually removed, so
    # untouched content passes through byte-for-byte.
    return result.strip(_INVISIBLE) if removed else text


def _strip_block(block: Any) -> Any:  # noqa: ANN401 - block shape is provider-defined
    """Sanitize one entry of a list-shaped content payload.

    Returns a NEW object for text-bearing blocks; the caller's block is never
    mutated. Unrecognised shapes pass through untouched rather than raising.
    """
    if isinstance(block, str):
        return _strip_text(block)
    if isinstance(block, dict) and isinstance(block.get("text"), str):
        return {**block, "text": _strip_text(block["text"])}
    return block


def strip_reasoning(content: str | list[Any]) -> str | list[Any]:
    """Strip leading ``<think>...</think>`` blocks from message content.

    Pure: no I/O, no mutation of the input. For list-shaped content the strip
    is applied to EVERY text-bearing block, each anchored at its own start —
    the invariant is that no reasoning text survives in any block.
    """
    if isinstance(content, str):
        return _strip_text(content)
    return [_strip_block(block) for block in content]
