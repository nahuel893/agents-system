"""Issue #188 -- a light core install, with everything else as extras.

A plain install of `agents-system` is the core a minimal agent needs: the
agent API, the harness, the in-process runtime and the audit and metrics
interfaces. The served app, the databases, the SQL tool, the embeddings and
the provider integrations are extras.

These tests prove it without a second environment. Each probe runs in a
fresh interpreter with an import blocker that makes the optional packages
look uninstalled -- what a core-only install sees -- and asserts that:

* the Quickstart and the README's library example run a full turn;
* a feature whose extra is missing fails with a message naming that extra.

`scripts/smoke-installed-wheel.sh` checks the same Quickstart against a real
core-only install of the built wheel.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
import textwrap
import tomllib

import pytest
from packaging.requirements import Requirement

REPO_ROOT = pathlib.Path(__file__).parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "agents_system"

#: Modules a core install does not provide, by the extra that does.
#: Written out here rather than read from `agents_system.extras`, so the
#: test does not take the implementation's word for it.
OPTIONAL_MODULES: dict[str, tuple[str, ...]] = {
    "server": (
        "fastapi",
        "starlette",
        "uvicorn",
        "redis",
        "langgraph.checkpoint.redis",
    ),
    "postgres": ("sqlalchemy", "asyncpg", "alembic"),
    "sql": ("sqlglot",),
    "embeddings": ("torch", "sentence_transformers", "transformers", "openai"),
    "openai": ("langchain_openai", "tiktoken"),
    "anthropic": ("langchain_anthropic", "anthropic"),
    "groq": ("langchain_groq", "groq"),
    "ollama": ("langchain_ollama", "ollama"),
}
EVERY_OPTIONAL_MODULE = tuple(
    sorted({module for modules in OPTIONAL_MODULES.values() for module in modules})
)

#: The distributions each extra must install directly (self-references such
#: as `agents-system[postgres]` are expanded before comparing).
EXPECTED_EXTRAS: dict[str, set[str]] = {
    "server": {"fastapi", "uvicorn", "redis", "langgraph-checkpoint-redis"},
    "postgres": {"sqlalchemy", "asyncpg", "alembic"},
    "sql": {"sqlglot"},
    "embeddings": {"torch", "sentence-transformers", "openai"},
    "openai": {"langchain-openai"},
    "anthropic": {"langchain-anthropic"},
    "groq": {"langchain-groq"},
    "ollama": {"langchain-ollama"},
}

#: Makes the listed modules look uninstalled in a fresh interpreter. Raising
#: from `find_spec` (instead of returning None) stops the search, exactly as
#: a missing package does.
_BLOCKER = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED = {blocked!r}

    class _NotInstalled(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            for module in BLOCKED:
                if name == module or name.startswith(module + "."):
                    raise ModuleNotFoundError(
                        f"No module named {{name!r}}", name=name
                    )
            return None

    sys.meta_path.insert(0, _NotInstalled())
    """
)


def _run_probe(
    blocked: tuple[str, ...], body: str, *args: str
) -> subprocess.CompletedProcess[str]:
    script = _BLOCKER.format(blocked=blocked) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        check=False,
    )


def _extract(document: str, kind: str) -> str:
    text = (REPO_ROOT / document).read_text(encoding="utf-8")
    after = text.split(f"<!-- runnable-example: {kind} -->", 1)[1].lstrip("\n")
    assert after.startswith("```python\n")
    return after[len("```python\n") :].split("\n```", 1)[0] + "\n"


# ---------------------------------------------------------------------------
# The core runs a turn
# ---------------------------------------------------------------------------
_RUN_EXAMPLE = """
import asyncio
import json

from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, ToolMessage


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


code, tool, arguments = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
model = ScriptedModel(
    responses=[
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call-1", "name": tool, "args": arguments, "type": "tool_call"}
            ],
        ),
        AIMessage(content="done"),
    ]
)
namespace = {"__name__": "example"}
exec(compile(code, "example", "exec"), namespace)
messages = asyncio.run(namespace["main"](model))
tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
assert len(tool_messages) == 1 and tool_messages[0].status != "error", messages
assert messages[-1].content == "done", messages
print("TURN-COMPLETED", tool_messages[0].content)
"""


@pytest.mark.parametrize(
    ("document", "kind", "tool", "arguments"),
    [
        (
            "docs/platform/library-usage.md",
            "quickstart",
            "word_count",
            {"text": "the quick brown fox"},
        ),
        ("README.md", "library", "catalog_search", {"query": "Item Alpha"}),
    ],
)
def test_a_documented_example_runs_a_turn_with_no_optional_package(
    document: str, kind: str, tool: str, arguments: dict[str, str]
) -> None:
    result = _run_probe(
        EVERY_OPTIONAL_MODULE,
        _RUN_EXAMPLE,
        _extract(document, kind),
        tool,
        json.dumps(arguments),
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert "TURN-COMPLETED" in result.stdout


def test_importing_every_public_name_needs_no_optional_package() -> None:
    result = _run_probe(
        EVERY_OPTIONAL_MODULE,
        """
        import agents_system

        for name in agents_system.__all__:
            getattr(agents_system, name)
        print("ALL-EXPORTS-IMPORTED")
        """,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert "ALL-EXPORTS-IMPORTED" in result.stdout


# ---------------------------------------------------------------------------
# A missing extra is named
# ---------------------------------------------------------------------------
_MISSING_EXTRA_CASES = [
    pytest.param(
        "server", ("fastapi",), "import agents_system.server", id="server-app"
    ),
    pytest.param("server", ("redis",), "import agents_system.main", id="server-main"),
    pytest.param(
        "server",
        ("fastapi",),
        "import agents_system.integration",
        id="server-integration",
    ),
    pytest.param(
        "server",
        ("starlette",),
        "import agents_system.channels.whatsapp",
        id="server-whatsapp-channel",
    ),
    pytest.param(
        "server",
        ("starlette",),
        "from agents_system.observability import RequestIdMiddleware",
        id="server-middleware",
    ),
    pytest.param(
        "postgres", ("sqlalchemy",), "import agents_system.models", id="postgres-models"
    ),
    pytest.param(
        "postgres",
        ("sqlalchemy",),
        "import agents_system\nagents_system.AuditSink(session_factory=object())",
        id="postgres-audit-sink",
    ),
    pytest.param(
        "postgres",
        ("asyncpg",),
        "import agents_system.services.reference",
        id="postgres-reference-backends",
    ),
    pytest.param(
        "sql",
        ("sqlglot",),
        "import agents_system.connectors.sql_query_connector",
        id="sql-connector",
    ),
    pytest.param(
        "embeddings",
        ("sentence_transformers",),
        "from agents_system.services.embeddings import LocalBGEEmbeddingProvider\n"
        "LocalBGEEmbeddingProvider()",
        id="embeddings-local",
    ),
    pytest.param(
        "embeddings",
        ("openai",),
        "from agents_system.services.embeddings import OpenAIEmbeddingProvider\n"
        "OpenAIEmbeddingProvider(api_key='k')",
        id="embeddings-openai",
    ),
    pytest.param(
        "openai",
        ("langchain_openai",),
        "from agents_system.agent.models import build_chat_model\n"
        "build_chat_model('openai_compatible')",
        id="provider-openai-compatible",
    ),
    pytest.param(
        "anthropic",
        ("langchain_anthropic",),
        "import agents_system\n"
        "agent = agents_system.Agent(\n"
        "    name='a', model={'provider': 'anthropic', 'model': 'claude-x'}\n"
        ")\n"
        "equipped = agents_system.build_runtime(\n"
        "    agent, agents_system.ToolRegistry(), granted_permissions=[]\n"
        ")\n"
        "agents_system.AgentRuntime(equipped)",
        id="provider-anthropic-declared-model",
    ),
    pytest.param(
        "groq",
        ("langchain_groq",),
        "from agents_system.agent.models import build_chat_model\n"
        "build_chat_model('groq')",
        id="provider-groq",
    ),
    pytest.param(
        "ollama",
        ("langchain_ollama",),
        "from agents_system.agent.models import build_chat_model\n"
        "build_chat_model('ollama')",
        id="provider-ollama",
    ),
]


@pytest.mark.parametrize(("extra", "blocked", "use"), _MISSING_EXTRA_CASES)
def test_a_feature_without_its_extra_fails_naming_the_extra(
    extra: str, blocked: tuple[str, ...], use: str
) -> None:
    result = _run_probe(
        blocked,
        f"""
        from agents_system.extras import MissingExtraError

        try:
{textwrap.indent(use, " " * 12)}
        except MissingExtraError as error:
            assert isinstance(error, ImportError)
            print("MISSING-EXTRA", error.extra)
            print(error)
        else:
            print("NO-ERROR")
        """,
    )

    assert result.returncode == 0, result.stderr[-4000:]
    assert f"MISSING-EXTRA {extra}" in result.stdout, result.stdout
    assert f"agents-system[{extra}]" in result.stdout
    assert blocked[0] in result.stdout, "the message names the missing module"


def test_an_unrelated_missing_module_is_not_reported_as_a_missing_extra() -> None:
    from agents_system.extras import MissingExtraError, raise_missing_extra

    unrelated = ModuleNotFoundError(
        "No module named 'not_an_extra'", name="not_an_extra"
    )

    with pytest.raises(ModuleNotFoundError) as caught:
        raise_missing_extra("server", "The served app", unrelated)

    assert caught.value is unrelated
    assert not isinstance(caught.value, MissingExtraError)


def test_the_message_says_how_to_install_the_extra() -> None:
    from agents_system.extras import MissingExtraError, raise_missing_extra

    with pytest.raises(MissingExtraError) as caught:
        raise_missing_extra(
            "sql",
            "The SQL query tool",
            ModuleNotFoundError("No module named 'sqlglot'", name="sqlglot"),
        )

    message = str(caught.value)
    assert message.startswith("The SQL query tool needs the 'sql' extra")
    assert "agents-system[sql]" in message
    assert "'sqlglot'" in message
    assert caught.value.name == "sqlglot"


# ---------------------------------------------------------------------------
# Every optional import is guarded
# ---------------------------------------------------------------------------
def _is_optional(module: str) -> bool:
    return any(
        module == optional or module.startswith(optional + ".")
        for optional in EVERY_OPTIONAL_MODULE
    )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.level == 0 and node.module else []
    return [alias.name for alias in node.names]


def _guards_with_missing_extra(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "raise_missing_extra"
        for call in ast.walk(handler)
    )


def _unguarded_optional_imports(
    body: list[ast.stmt], guarded: bool
) -> list[ast.Import | ast.ImportFrom]:
    """Module-level imports of optional packages outside a guard.

    A guard is a `try` whose `except` calls `raise_missing_extra`. Imports
    under `if TYPE_CHECKING:` never run; imports inside functions are lazy
    and covered by the probes above.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            if not guarded and any(map(_is_optional, _imported_modules(node))):
                found.append(node)
        elif isinstance(node, ast.If):
            if ast.unparse(node.test) not in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                found += _unguarded_optional_imports(node.body, guarded)
                found += _unguarded_optional_imports(node.orelse, guarded)
        elif isinstance(node, ast.Try):
            is_guard = any(map(_guards_with_missing_extra, node.handlers))
            found += _unguarded_optional_imports(node.body, guarded or is_guard)
    return found


def test_every_module_level_optional_import_is_guarded() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _unguarded_optional_imports(tree.body, guarded=False):
            relative = path.relative_to(REPO_ROOT)
            offenders.append(f"{relative}:{node.lineno}: {ast.unparse(node)}")

    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# pyproject.toml declares the core and the extras
# ---------------------------------------------------------------------------
def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _expand(extra: str, extras: dict[str, list[str]]) -> set[str]:
    """Every distribution `extra` installs directly, self-references expanded."""
    names: set[str] = set()
    for line in extras[extra]:
        requirement = Requirement(line)
        if requirement.name == "agents-system":
            for included in requirement.extras:
                names |= _expand(included, extras)
        else:
            names.add(requirement.name)
    return names


def test_the_core_dependencies_leave_out_every_extra() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    core = {Requirement(line).name for line in project["dependencies"]}
    optional = set().union(*EXPECTED_EXTRAS.values())

    assert core.isdisjoint(optional), sorted(core & optional)
    # What the core imports directly is declared, not left to chance.
    assert {"langgraph", "langchain-core", "pydantic", "pyyaml"} <= core


@pytest.mark.parametrize("extra", sorted(EXPECTED_EXTRAS))
def test_each_extra_installs_its_feature(extra: str) -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]

    assert EXPECTED_EXTRAS[extra] <= _expand(extra, extras)


def test_the_served_app_extra_brings_its_database() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)

    assert EXPECTED_EXTRAS["postgres"] <= _expand(
        "server", project["optional-dependencies"]
    )


def test_all_installs_every_extra() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]

    assert set().union(*EXPECTED_EXTRAS.values()) <= _expand("all", extras)


def test_the_development_environment_installs_all() -> None:
    """`uv sync` (the dev group) is what the offline suite runs against."""
    groups = _pyproject()["dependency-groups"]
    assert isinstance(groups, dict)
    requirements = [Requirement(line) for line in groups["dev"]]

    assert any(
        requirement.name == "agents-system" and "all" in requirement.extras
        for requirement in requirements
    )


def test_the_code_names_only_declared_extras() -> None:
    from agents_system.extras import EXTRA_MODULES

    project = _pyproject()["project"]
    assert isinstance(project, dict)

    assert set(EXTRA_MODULES) <= set(project["optional-dependencies"])
    for extra, modules in OPTIONAL_MODULES.items():
        assert set(modules) <= EXTRA_MODULES[extra], extra
