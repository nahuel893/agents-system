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

import asyncio
import contextlib
import os
import pathlib
import random
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agents_system.connectors.command_tools import build_command_tool_connector
from agents_system.connectors.operator import (
    SandboxPolicy,
    TerminalPolicy,
    _kill_group,
    build_terminal_connector,
)
from agents_system.harness.loader import CommandToolDeclaration, Tier

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
    sentinel = pathlib.Path.home() / ".agents_system-sandbox-test-sentinel"
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
    target = pathlib.Path("/usr/agents_system-sandbox-escape-write.txt")
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
    monkeypatch.setenv("AGENTS_SYSTEM_SANDBOX_TEST_SECRET", "must-not-leak")
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-c",
                (
                    "import os; "
                    "print(os.environ.get('AGENTS_SYSTEM_SANDBOX_TEST_SECRET', 'ABSENT'))"
                ),
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


def _sleeps_still_running(duration: str) -> list[str]:
    """Host pids of `sleep <duration>` -- a unique duration is the marker."""
    found = []
    for pid_dir in pathlib.Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            argv = (pid_dir / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:1] and argv[0].endswith(b"sleep") and duration.encode() in argv:
            found.append(pid_dir.name)
    return found


@pytest.mark.parametrize("tool", ["use_term", "command_tool"])
async def test_a_sandboxed_command_is_killed_when_an_outer_timeout_cancels_it(
    tool: str, tmp_path: pathlib.Path
) -> None:
    """#105: the interceptor's `tool_call_timeout_s` fires before the
    sandbox's own kill timer -- the shape of
    `evals/scenarios/mechanism/06_tool_call_timeout.yaml`, with the same
    real `sleep`. The cancellation used to reach `_run_argv` as
    `CancelledError`, skip its `except TimeoutError` kill, and leave `sleep`
    running inside `bwrap` on the host."""
    import asyncio
    import random
    import shutil

    duration = f"30.{random.randrange(10**8):08d}"
    if tool == "use_term":
        policy = _policy(tmp_path, allowed_commands=frozenset({"sleep"}))
        use_term = build_terminal_connector(policy)
        call = use_term({"argv": ["sleep", duration]})
    else:
        sleep_path = shutil.which("sleep")
        assert sleep_path is not None
        declaration = CommandToolDeclaration(
            name="nap",
            argv=(sleep_path, duration),
            params={},
            tier=Tier.T2,
            permission="run:nap",
        )
        call = build_command_tool_connector(declaration)({})

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1.0):
            await call

    survivors = _sleeps_still_running(duration)
    for _ in range(20):
        if not survivors:
            break
        await asyncio.sleep(0.1)
        survivors = _sleeps_still_running(duration)
    assert not survivors, f"`sleep {duration}` outlived the timeout: {survivors}"


# ---------------------------------------------------------------------------
# #105 follow-up — the kill must reach the sandbox during bwrap's start-up
# ---------------------------------------------------------------------------
#
# `_run_argv` kills the launcher's process group. `bwrap --new-session` used
# to move the namespace init, and the command it forks, into a session and
# group of their own, so they died only through `--die-with-parent`. bwrap
# arms that on the namespace init after its `setsid()`, and a kill landing
# in between orphaned the whole sandbox, command included, on the host.


def _proc_stat(pid: int) -> tuple[int, int, int] | None:
    """`(ppid, pgid, sid)` of *pid* as the host sees it, or None once gone."""
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    ppid, pgid, sid = raw.rsplit(")", 1)[1].split()[1:4]
    return int(ppid), int(pgid), int(sid)


def _children(pid: int) -> list[int]:
    try:
        text = pathlib.Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    return [int(child) for child in text.split()]


def _holds(pid: int, marker: str) -> bool:
    try:
        argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return marker.encode() in argv


def _carrying(marker: str) -> list[int]:
    """Host pids whose argv holds *marker*: the launcher, bwrap's namespace
    init (bwrap forks it without an exec) and the command itself."""
    pids = (int(d.name) for d in pathlib.Path("/proc").iterdir() if d.name.isdigit())
    return [pid for pid in pids if _holds(pid, marker)]


def _unique_duration() -> str:
    return f"30.{random.randrange(10**8):08d}"


async def test_every_process_the_sandbox_runs_is_in_the_group_the_kill_targets(
    tmp_path: pathlib.Path,
) -> None:
    """The invariant that makes the kill independent of bwrap's timing: the
    launcher leads its own group, and the namespace init and the command
    are in it, so one `killpg` reaches all of them at any moment."""
    duration = _unique_duration()
    use_term = build_terminal_connector(
        _policy(tmp_path, allowed_commands=frozenset({"sleep"}))
    )
    call = asyncio.create_task(use_term({"argv": ["sleep", duration]}))
    try:
        for _ in range(100):
            sleeps = _sleeps_still_running(duration)
            if sleeps:
                break
            await asyncio.sleep(0.05)
        assert sleeps, f"`sleep {duration}` never started"

        chain = [int(sleeps[0])]
        while (stat := _proc_stat(chain[-1])) is not None and stat[0] != os.getpid():
            chain.append(stat[0])
        launcher = chain[-1]
        seen = {pid: _proc_stat(pid) for pid in chain}

        assert len(chain) == 3, f"expected launcher, namespace init, command: {seen}"
        outside = {
            pid: stat
            for pid, stat in seen.items()
            if stat is None or stat[1] != launcher
        }
        assert not outside, (
            f"outside the launcher's group {launcher}, as (ppid, pgid, sid): {outside}"
        )
    finally:
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call


async def _launcher_of(marker: str) -> int:
    """The spawned launcher: this process's child whose argv holds *marker*."""
    for _ in range(100_000):
        for child in _children(os.getpid()):
            if _holds(child, marker):
                return child
        await asyncio.sleep(0)
    raise AssertionError(f"no launcher for {marker} appeared")


def _spin_until_start_up_milestone(launcher: int) -> None:
    """Return at the first sign of either: a child of the launcher outside
    its group, the moment the old kill missed, or that child having forked
    the command. Spins without yielding, because the gap is microseconds."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        for init in _children(launcher):
            stat = _proc_stat(init)
            if stat is not None and stat[1] != launcher:
                return
            if _children(init):
                return


async def test_a_kill_while_bwrap_is_still_starting_leaves_nothing_running(
    tmp_path: pathlib.Path,
) -> None:
    """Delivers `_run_argv`'s own kill, `_kill_group`, at the start-up
    moment it used to miss. Cancelling the call takes a few hundred
    microseconds to reach that kill, longer than the gap, so the test
    delivers it directly. Repeated because the gap is short: with
    `--new-session`, a kill there leaked the sandbox in about 4 of 10
    attempts on a developer laptop."""
    use_term = build_terminal_connector(
        _policy(tmp_path, allowed_commands=frozenset({"sleep"}), timeout_s=3.0)
    )
    leaked: dict[str, list[int]] = {}
    for _ in range(10):
        duration = _unique_duration()
        call = asyncio.create_task(use_term({"argv": ["sleep", duration]}))
        launcher = await _launcher_of(duration)
        _spin_until_start_up_milestone(launcher)
        _kill_group(SimpleNamespace(pid=launcher))
        await call

        survivors = _carrying(duration)
        for _ in range(20):
            if not survivors:
                break
            await asyncio.sleep(0.1)
            survivors = _carrying(duration)
        if survivors:
            leaked[duration] = survivors
            for pid in survivors:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)

    assert not leaked, f"sandboxes outlived the kill, by marker: {leaked}"


_TERMINAL_PROBE = (
    "import os\n"
    "try:\n"
    "    os.open('/dev/tty', os.O_RDWR)\n"
    "    print('TERMINAL_OPENED')\n"
    "except OSError as error:\n"
    "    print('NO_TERMINAL', error.errno)\n"
)

_TERMINAL_HELPER = """\
import asyncio, os, pathlib, sys
os.setsid()
os.open(sys.argv[1], os.O_RDWR)  # a session leader's first tty: its terminal
os.close(os.open("/dev/tty", os.O_RDWR))  # so this helper does have one
from agents_system.connectors.operator import (
    SandboxPolicy, TerminalPolicy, build_terminal_connector)
use_term = build_terminal_connector(TerminalPolicy(
    root=pathlib.Path(sys.argv[2]),
    allowed_commands=frozenset({sys.executable}),
    sandbox=SandboxPolicy(),
))
result = asyncio.run(use_term({"argv": [sys.executable, "-c", sys.argv[3]]}))
print(result)
"""


def test_a_sandboxed_command_cannot_open_the_callers_terminal(
    tmp_path: pathlib.Path,
) -> None:
    """What `--new-session` guarded against, kept without it: the launcher
    starts a session of its own, so the sandbox has no controlling terminal
    and cannot open the caller's to push input into it with `TIOCSTI`
    (CVE-2017-5226). The call runs from a helper that does have one, a pty,
    so this fails if the sandbox ever inherits it."""
    import agents_system

    master, slave = os.openpty()
    try:
        terminal = os.ttyname(slave)
        os.close(slave)
        src = pathlib.Path(agents_system.__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _TERMINAL_HELPER,
                terminal,
                str(tmp_path),
                _TERMINAL_PROBE,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(src)},
            check=False,
        )
    finally:
        os.close(master)

    assert completed.returncode == 0, completed.stderr
    assert "NO_TERMINAL" in completed.stdout, completed.stdout
    assert "TERMINAL_OPENED" not in completed.stdout


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

    prefix = str(pathlib.Path(tempfile.gettempdir()) / "agents_system-command-tool-*")
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

    import agents_system.connectors.command_tools as command_tools_module

    monkeypatch.setattr(command_tools_module, "_DEFAULT_TIMEOUT_S", 1.0)
    prefix = str(pathlib.Path(tempfile.gettempdir()) / "agents_system-command-tool-*")
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
