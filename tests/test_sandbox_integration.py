# pyright: reportMissingImports=false, reportCallIssue=false, reportArgumentType=false
"""ADR-002 C.14 — real bubblewrap sandbox integration proofs.

Every test here needs a REAL `bwrap` on the host and actually spawns
sandboxed subprocesses -- that is why the whole module is `integration`
-marked (deselected by default; run explicitly via `pytest -m integration`,
which the dedicated `sandbox-integration` CI job does). `test_operator_
connectors.py` and `test_command_tools_connector.py` already exercise this
same code path for their full existing suites, since sandboxing is
unconditional now, not opt-in -- this module exists for the SPECIFIC
isolation properties C.14 promises: no network, no filesystem escape in
either direction, no leaked host env, a bounded memory ceiling, and a real
kill at timeout.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from agentsys.connectors.command_tools import build_command_tool_connector
from agentsys.connectors.operator import (
    SandboxPolicy,
    TerminalPolicy,
    build_terminal_connector,
)
from agentsys.harness.loader import CommandToolDeclaration, Tier

pytestmark = pytest.mark.integration

_NETWORK_PROBE = (
    "import socket\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('1.1.1.1', 80))\n"
    "    print('REACHED')\n"
    "except OSError as e:\n"
    "    print('BLOCKED', e)\n"
)


def _policy(root: pathlib.Path, **kw: object) -> TerminalPolicy:
    defaults: dict[str, object] = {
        "root": root,
        "allowed_commands": frozenset({sys.executable}),
        "sandbox": SandboxPolicy(),
    }
    defaults.update(kw)
    return TerminalPolicy(**defaults)  # type: ignore[arg-type]


async def test_a_sandboxed_command_cannot_reach_the_network(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": [sys.executable, "-c", _NETWORK_PROBE]})

    assert "REACHED" not in result["stdout"], "the sandbox did not block the network"
    assert "BLOCKED" in result["stdout"]


async def test_a_sandboxed_command_cannot_read_outside_the_workspace(
    tmp_path: pathlib.Path,
) -> None:
    """A sentinel outside the root, the shape a `.env` leak would take."""
    sentinel = pathlib.Path.home() / ".agentsys-sandbox-test-sentinel"
    sentinel.write_text("do-not-leak", encoding="utf-8")
    try:
        connector = build_terminal_connector(_policy(tmp_path))

        result = await connector(
            {"argv": [sys.executable, "-c", f"print(open({str(sentinel)!r}).read())"]}
        )

        assert "do-not-leak" not in result["stdout"]
        assert result["exit_code"] != 0
    finally:
        sentinel.unlink(missing_ok=True)


async def test_a_sandboxed_command_cannot_write_outside_the_workspace(
    tmp_path: pathlib.Path,
) -> None:
    """A target under `/usr` -- a REAL read-only bind, not a directory that
    merely got auto-created as scaffolding for some other bind (any such
    scaffold directory IS writable, since it is a plain, sandbox-internal
    placeholder, not bound to any real host path -- a write there lands in
    the sandbox's own discarded overlay, not on host disk, which is already
    safe but gives no clean OS-level failure to assert on).
    """
    target = pathlib.Path("/usr/agentsys-sandbox-escape-write.txt")
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-c",
                f"open({str(target)!r}, 'w').write('escaped')",
            ]
        }
    )

    assert result["exit_code"] != 0
    assert not target.exists(), "the sandbox did not block the write"


async def test_a_sandboxed_command_does_not_see_host_only_env_vars(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beyond `_child_env`'s own scrubbing: `--clearenv` inside the sandbox
    itself, proven with a var that only exists on the HOST side."""
    monkeypatch.setenv("AGENTSYS_SANDBOX_TEST_SECRET", "must-not-leak")
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-c",
                "import os; "
                "print(os.environ.get('AGENTSYS_SANDBOX_TEST_SECRET', 'ABSENT'))",
            ]
        }
    )

    assert "ABSENT" in result["stdout"]
    assert "must-not-leak" not in result["stdout"]


async def test_a_sandboxed_command_is_killed_at_timeout(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path, timeout_s=1.0))

    result = await connector(
        {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    )

    assert result.get("error_kind") == "timeout"


async def test_a_sandboxed_command_hitting_the_memory_limit_is_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """`bwrap` has no resource-limit flags of its own (namespaces/mounts
    only) -- `RLIMIT_AS`, applied via `preexec_fn`, is what bounds memory."""
    connector = build_terminal_connector(
        _policy(tmp_path, sandbox=SandboxPolicy(max_memory_bytes=64 * 1024 * 1024))
    )

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-c",
                "x = bytearray(500 * 1024 * 1024); print('allocated', len(x))",
            ]
        }
    )

    assert result["exit_code"] != 0
    assert "allocated" not in result["stdout"]


async def test_a_declared_command_tool_runs_inside_the_sandbox_too() -> None:
    """ADR-002 C.14 covers `command_tools`, not just `use_term` -- both call
    through the shared `_run_argv` seam, so one sandbox wraps both."""
    declaration = CommandToolDeclaration(
        name="probe_network",
        argv=(sys.executable, "-c", _NETWORK_PROBE),
        params={},
        tier=Tier.T2,
        permission="run:probe_network",
    )
    connector = build_command_tool_connector(declaration)

    result = await connector({})
    assert "REACHED" not in result["stdout"]


# ---------------------------------------------------------------------------
# ADR-002 C.14 review follow-up (PR #148, BLOCKER) — command_tools' own
# scratch workspace, never the process cwd
# ---------------------------------------------------------------------------
#
# `run_command_tool` used to hardcode `TerminalPolicy(root=Path.cwd(), ...)`,
# and C.14 binds `root` READ-WRITE inside the sandbox: a reviewer used a
# sandboxed command tool to read a sentinel `.env` (with its token contents)
# and to overwrite both it and `.git/config` in a stand-in repo directory --
# exactly the access this ADR exists to forbid. The fix gives every call its
# own fresh `tempfile.mkdtemp()` workspace, removed in a `finally`.


def _read_dotenv_declaration(target: pathlib.Path) -> CommandToolDeclaration:
    return CommandToolDeclaration(
        name="read_dotenv",
        argv=(sys.executable, "-c", f"print(open({str(target)!r}).read())"),
        params={},
        tier=Tier.T2,
        permission="run:read_dotenv",
    )


def _write_dotenv_declaration(
    target: pathlib.Path, payload: str
) -> CommandToolDeclaration:
    return CommandToolDeclaration(
        name="write_dotenv",
        argv=(
            sys.executable,
            "-c",
            f"open({str(target)!r}, 'w').write({payload!r})",
        ),
        params={},
        tier=Tier.T2,
        permission="run:write_dotenv",
    )


async def test_command_tool_cannot_read_a_sentinel_dotenv_in_the_process_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in repo dir with a `.env` holding a token -- the exact shape
    the reviewer used to demonstrate the leak. `command_tools` must not be
    able to read it via its real, absolute path, even though it used to be
    the process's own working directory."""
    repo_dir = tmp_path / "stand-in-repo"
    repo_dir.mkdir()
    dotenv = repo_dir / ".env"
    dotenv.write_text("API_TOKEN=do-not-leak-12345\n", encoding="utf-8")
    monkeypatch.chdir(repo_dir)

    connector = build_command_tool_connector(_read_dotenv_declaration(dotenv))
    result = await connector({})

    assert "do-not-leak-12345" not in result.get("stdout", "")
    assert result["exit_code"] != 0


async def test_command_tool_cannot_write_a_sentinel_dotenv_in_the_process_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "stand-in-repo"
    repo_dir.mkdir()
    dotenv = repo_dir / ".env"
    dotenv.write_text("API_TOKEN=untouched\n", encoding="utf-8")
    monkeypatch.chdir(repo_dir)

    connector = build_command_tool_connector(
        _write_dotenv_declaration(dotenv, "OVERWRITTEN")
    )
    result = await connector({})

    assert result["exit_code"] != 0
    assert dotenv.read_text(encoding="utf-8") == "API_TOKEN=untouched\n"


async def test_command_tool_cannot_overwrite_git_config_in_the_process_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "stand-in-repo"
    (repo_dir / ".git").mkdir(parents=True)
    git_config = repo_dir / ".git" / "config"
    git_config.write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    monkeypatch.chdir(repo_dir)

    connector = build_command_tool_connector(
        _write_dotenv_declaration(git_config, "[core]\n\tpwned = true\n")
    )
    result = await connector({})

    assert result["exit_code"] != 0
    assert "pwned" not in git_config.read_text(encoding="utf-8")


async def test_command_tool_scratch_workspace_is_writable_and_cleaned_up(
    tmp_path: pathlib.Path,
) -> None:
    """The scratch workspace itself IS writable (a command tool needs
    somewhere to run) and is removed once the call completes."""
    import glob
    import tempfile

    prefix = str(pathlib.Path(tempfile.gettempdir()) / "agentsys-command-tool-*")
    before = set(glob.glob(prefix))

    declaration = CommandToolDeclaration(
        name="touch_marker",
        argv=(
            sys.executable,
            "-c",
            "open('marker.txt', 'w').write('x'); print('wrote')",
        ),
        params={},
        tier=Tier.T2,
        permission="run:touch_marker",
    )
    result = await build_command_tool_connector(declaration)({})

    after = set(glob.glob(prefix))
    assert result["exit_code"] == 0
    assert "wrote" in result["stdout"]
    assert after == before, f"scratch workspace(s) leaked: {after - before}"


async def test_command_tool_scratch_workspace_is_cleaned_up_after_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import glob
    import tempfile

    import agentsys.connectors.command_tools as command_tools_module

    monkeypatch.setattr(command_tools_module, "_DEFAULT_TIMEOUT_S", 1.0)
    prefix = str(pathlib.Path(tempfile.gettempdir()) / "agentsys-command-tool-*")
    before = set(glob.glob(prefix))

    declaration = CommandToolDeclaration(
        name="hang",
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        params={},
        tier=Tier.T2,
        permission="run:hang",
    )
    result = await build_command_tool_connector(declaration)({})

    after = set(glob.glob(prefix))
    assert result["error_kind"] == "timeout"
    assert after == before, (
        f"scratch workspace(s) leaked after timeout: {after - before}"
    )
