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

LIMITATIONS, all deliberate and covered by tests that assert the current
behavior so they stay intentional rather than becoming surprises:

- Streaming is NOT sanitized. ``_stream``/``_astream`` are not overridden,
  because nothing in this system streams today (the OpenAI adapter rejects
  ``stream:true`` outright and the graph uses ``ainvoke``). Sanitizing a stream
  means tracking tag boundaries across chunks. If streaming is ever enabled,
  reasoning WILL leak until that is handled.
- Only the exact lowercase, bare ``<think>`` spelling is recognised. Uppercase
  (``<THINK>``) and attributed (``<think type="x">``) variants pass through, as
  does a stray ``</think>`` with no opener. None of these have been observed
  from MiniMax; generalising to other conventions belongs with the wider
  provider work, not here.
- Reasoning inside a tool call's arguments is not touched. Those arguments are
  consumed by connectors and never shown to a user, and rewriting them could
  corrupt a valid tool invocation.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

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

    Matching the LAST closing tag would also fix nesting — and would swallow a
    legitimate answer that merely mentions the tag further down. Counting depth
    stops at the tag that actually closes the leading block, which is the only
    option that handles both.

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


class ReasoningSanitizedChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` that strips leading reasoning blocks from its responses.

    This MUST stay a ``ChatOpenAI`` subclass. ``agent/graph.py`` calls
    ``.bind_tools(...)`` on whatever the model factory hands it, and a composed
    ``model | parser`` would be a ``RunnableSequence`` with no ``bind_tools`` at
    all. Subclassing keeps the whole chat-model surface intact and keeps the
    override on the call path even once tools are bound.

    Stateless: the app builds one instance at startup and shares it across
    concurrent requests, so nothing here may hold per-request state.
    """

    def _sanitize(self, result: ChatResult) -> ChatResult:
        """Return a copy of ``result`` with reasoning stripped from content.

        Rebuilds each generation instead of mutating it. ``ChatGeneration.text``
        is snapshotted from ``message.content`` when the generation is
        constructed and does NOT follow a later in-place assignment — mutating
        would leave the raw reasoning readable through ``.text``.

        ``tool_calls`` are deliberately untouched: their arguments are consumed
        by connectors, never shown to the user, and rewriting them would risk
        corrupting a valid tool invocation.
        """
        generations: list[ChatGeneration] = []
        for generation in result.generations:
            message = generation.message
            cleaned = strip_reasoning(message.content)
            if cleaned == message.content:
                generations.append(generation)
                continue
            generations.append(
                ChatGeneration(
                    message=message.model_copy(update={"content": cleaned}),
                    generation_info=generation.generation_info,
                )
            )
        return ChatResult(generations=generations, llm_output=result.llm_output)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._sanitize(super()._generate(messages, stop, run_manager, **kwargs))

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._sanitize(
            await super()._agenerate(messages, stop, run_manager, **kwargs)
        )
