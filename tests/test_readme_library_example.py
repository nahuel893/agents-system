"""Issue #166 -- the documented library example runs exactly as written.

The README's library-first example used to register only `catalog_search`
and build `sales-agent`, whose manifest declares four more tools (one of
them, `escalation_notifier`, inherited from its parent role), so
`build_runtime` raised `InjectionError: Unknown tool: escalation_notifier`.
Nothing executed the snippet, so nothing noticed.

This test executes the marked example block of each document verbatim --
the only thing it supplies is a scripted chat model, passed to the
example's own `main(model)` -- with every server environment variable
cleared, so the docs cannot drift from what actually runs.
`scripts/smoke-installed-wheel.sh` runs the same README block against the
built wheel in an empty environment.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, ToolMessage

from agents_system.config import Settings, get_settings
from agents_system.harness.loader import PredefinedRoleDeprecationWarning

REPO_ROOT = pathlib.Path(__file__).parent.parent

#: Placed on the line right before a ```python fence to mark it runnable.
EXAMPLE_MARKER = "<!-- runnable-example: library -->"

DOCUMENTS = (
    "README.md",
    "docs/platform/library-usage.md",
    "docs/platform_es/library-usage.md",
)


def extract_example(document: pathlib.Path) -> str:
    """The python block that follows `EXAMPLE_MARKER` -- exactly one."""
    text = document.read_text(encoding="utf-8")
    assert text.count(EXAMPLE_MARKER) == 1, (
        f"{document} must mark exactly one runnable example"
    )
    after_marker = text.split(EXAMPLE_MARKER, 1)[1].lstrip("\n")
    assert after_marker.startswith("```python\n"), (
        f"{document}: the marker must sit right above a ```python fence"
    )
    body = after_marker[len("```python\n") :]
    return body.split("\n```", 1)[0] + "\n"


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ScriptedModel:
        return self


@pytest.fixture
def no_server_environment(monkeypatch: pytest.MonkeyPatch) -> Any:
    server_fields = set(Settings.model_fields)
    for name in list(os.environ):
        if name.lower() in server_fields:
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("document", DOCUMENTS)
async def test_the_documented_library_example_runs_as_written(
    document: str, no_server_environment: None
) -> None:
    code = extract_example(REPO_ROOT / document)
    namespace: dict[str, Any] = {"__name__": "documented_example"}
    # ADR-010 (#222): the example builds `sales-agent` by name, which is
    # deprecated until the docs move off the concrete roles (#229).
    with pytest.warns(
        PredefinedRoleDeprecationWarning, match="examples/agents/sales-agent/"
    ):
        exec(compile(code, document, "exec"), namespace)  # noqa: S102

    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "catalog_search",
                        "args": {"query": "Item Alpha"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Yes, Item Alpha is in stock."),
        ]
    )
    messages = await namespace["main"](model)

    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "Item Alpha" in str(tool_messages[0].content)
    assert messages[-1].content == "Yes, Item Alpha is in stock."
