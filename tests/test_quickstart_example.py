"""Issue #168 -- the library Quickstart runs exactly as written.

`docs/platform/library-usage.md` and its Spanish twin open with a Quickstart:
an agent with one custom tool, and nothing else registered, running a full
turn. This test executes the marked block of each document verbatim, the same
way `tests/test_readme_library_example.py` does for the complete example: the
only thing it supplies is a scripted chat model, passed to the block's own
`main(model)`, with every server environment variable cleared.
`scripts/smoke-installed-wheel.sh` runs the same block against the built
wheel, installed outside the repository.
"""

from __future__ import annotations

import ast
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

REPO_ROOT = pathlib.Path(__file__).parent.parent

QUICKSTART_MARKER = "<!-- runnable-example: quickstart -->"

DOCUMENTS = (
    "docs/platform/library-usage.md",
    "docs/platform_es/library-usage.md",
)

SCRIPT = "examples/quickstart.py"

#: "About ten lines of user code", counted as statements so that line
#: wrapping does not matter. Imports are not counted.
MAX_STATEMENTS = 12


def extract_quickstart(document: pathlib.Path) -> str:
    text = document.read_text(encoding="utf-8")
    assert text.count(QUICKSTART_MARKER) == 1, (
        f"{document} must mark exactly one runnable quickstart"
    )
    after_marker = text.split(QUICKSTART_MARKER, 1)[1].lstrip("\n")
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
async def test_the_quickstart_runs_as_written(
    document: str, no_server_environment: None
) -> None:
    code = extract_quickstart(REPO_ROOT / document)
    namespace: dict[str, Any] = {"__name__": "quickstart"}
    exec(compile(code, document, "exec"), namespace)  # noqa: S102

    # One custom tool and nothing else: the escalation tool the parent role
    # declares is the platform's built-in default, not the reader's job.
    assert namespace["registry"].names() == ("word_count",)

    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "word_count",
                        "args": {"text": "the quick brown fox"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="There are 4 words."),
        ]
    )
    messages = await namespace["main"](model)

    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert '"words": 4' in str(tool_messages[0].content)
    assert messages[-1].content == "There are 4 words."


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_quickstart_stays_about_ten_lines(document: str) -> None:
    tree = ast.parse(extract_quickstart(REPO_ROOT / document))
    statements = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and not isinstance(node, ast.Import | ast.ImportFrom)
    ]

    assert len(statements) <= MAX_STATEMENTS, ast.unparse(tree)


def test_both_documents_carry_the_same_quickstart_code() -> None:
    """Only comments may differ between the English and Spanish copies."""
    english, spanish = (
        ast.dump(ast.parse(extract_quickstart(REPO_ROOT / document)))
        for document in DOCUMENTS
    )

    assert english == spanish


def test_the_example_script_is_the_documented_quickstart() -> None:
    """Issue #315 -- `examples/quickstart.py` is the block above as a file,
    so the tests that run the block cover the script too. Only comments may
    differ."""
    script = (REPO_ROOT / SCRIPT).read_text(encoding="utf-8")
    documented = extract_quickstart(REPO_ROOT / DOCUMENTS[0])

    assert ast.dump(ast.parse(script)) == ast.dump(ast.parse(documented))
