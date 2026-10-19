"""Live scenario for `agents chat` (issue #312, opt-in, `-m live`).

`agents chat` answers one turn that needs a tool, on a real model: the tool
returns a code the model cannot know, so a reply carrying it proves the turn
went through the tool, and the shown tool call proves the command displayed it.
The command runs as an operator runs it: its own process, its input piped in,
from a project directory outside the repository.

The model is `EVAL_PROVIDER`'s, with that provider's own variables (for the
reference model, `OPENAI_COMPATIBLE_*`). Run with:
`uv run --env-file .env pytest -m live tests/test_live_cli_chat.py -rA`
(see docs/platform/live-eval.md).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import uuid

import pytest

pytestmark = pytest.mark.live

#: Trials, the same `EVAL_RUNS` knob the other live modules read.
_RUNS = int(os.environ.get("EVAL_RUNS", "3"))

_REGISTRY_MODULE = textwrap.dedent(
    """
    import agents_system


    def stock_status(inputs, /):
        return {{"item": inputs["item_id"], "status_code": {code!r}}}


    registry = agents_system.ToolRegistry()
    registry.register(
        agents_system.ToolSpec(
            name="stock_status",
            description="Look up the stock status code of a catalog item by its id.",
            input_schema={{
                "type": "object",
                "properties": {{"item_id": {{"type": "string"}}}},
                "required": ["item_id"],
            }},
            required_permissions=("read:catalog",),
            connector=stock_status,
            tier=agents_system.Tier.T1,
        )
    )
    """
)


def _write_project(base: pathlib.Path, code: str) -> None:
    (base / "live_tools.py").write_text(
        _REGISTRY_MODULE.format(code=code), encoding="utf-8"
    )
    agent = base / "support"
    agent.mkdir()
    (agent / "role.md").write_text(
        '---\nname: support\nversion: "1.0"\n---\n\n# Role: support\n\n'
        "You answer questions about catalog items. Use the stock_status tool "
        "to look an item up; never guess a status code.\n",
        encoding="utf-8",
    )
    (agent / "manifest.md").write_text(
        '---\nrole: support\nversion: "1.0"\nextends: agent\n'
        "tools: [stock_status]\nskills: []\ncontext: {}\n"
        "permissions: [read:catalog]\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (agent / "policy.md").write_text(
        '---\nrole: support\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nBody.\n",
        encoding="utf-8",
    )


def test_agents_chat_answers_a_turn_that_needs_a_tool(tmp_path: pathlib.Path) -> None:
    provider = os.environ.get("EVAL_PROVIDER")
    if not provider:
        pytest.fail(
            "EVAL_PROVIDER is not set: a live run names its model "
            "(see docs/platform/live-eval.md)"
        )

    passed = 0
    failures: list[str] = []
    for trial in range(_RUNS):
        code = f"ST-{uuid.uuid4().hex[:8].upper()}"
        project = tmp_path / f"trial-{trial}"
        project.mkdir()
        _write_project(project, code)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agents_system",
                "chat",
                "./support",
                "--provider",
                provider,
                "--registry",
                "live_tools:registry",
                "--grant",
                "read:catalog",
            ],
            cwd=project,
            input="What is the stock status code of item 4471? Reply with the code.\nexit\n",
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        print(
            f"trial {trial}: {result.stdout.splitlines()[0] if result.stdout else ''}"
        )
        ok = (
            result.returncode == 0
            and "-> stock_status(" in result.stdout
            and "<- stock_status: " in result.stdout
            and any(
                line.startswith("agent> ") and code in line
                for line in result.stdout.splitlines()
            )
        )
        if ok:
            passed += 1
        else:
            failures.append(f"trial {trial}:\n{result.stdout}\n{result.stderr[-2000:]}")

    print(f"agents chat tool turn: {passed}/{_RUNS} passed")
    assert passed == _RUNS, "\n\n".join(failures)
