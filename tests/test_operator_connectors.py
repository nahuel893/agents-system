# type: ignore
# pyright: reportMissingImports=false, reportCallIssue=false, reportArgumentType=false
"""The two basic operator tools, and the boundaries that make them shippable.

`use_term` is arbitrary code execution and `read_file` is arbitrary data
exfiltration from the host. Both fail CLOSED: an unconfigured policy runs
nothing and reads nothing, so a deployment that forgets to configure them
gets an inert tool rather than an open one.

The tests that matter here are the escape attempts. A happy-path test proves
the tool works; only these prove it is safe to have.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

from agents_system.connectors.operator import (
    SandboxPolicy,
    TerminalPolicy,
    build_file_reader_connector,
    build_terminal_connector,
)


def _policy(root: pathlib.Path, **kw: object) -> TerminalPolicy:
    defaults: dict[str, object] = {
        "root": root,
        "allowed_commands": frozenset({sys.executable, "echo"}),
        "sandbox": SandboxPolicy(),
    }
    defaults.update(kw)
    return TerminalPolicy(**defaults)  # type: ignore[arg-type]


# --- use_term: fails closed ------------------------------------------------


async def test_an_unconfigured_allowlist_runs_nothing(tmp_path: pathlib.Path) -> None:
    """The default is inert, not open.

    A deployment that declares the tool and forgets the policy must get a
    refusal, never a shell. This is the single most important assertion in
    the file.
    """
    connector = build_terminal_connector(
        TerminalPolicy(
            root=tmp_path, allowed_commands=frozenset(), sandbox=SandboxPolicy()
        )
    )

    result = await connector({"argv": ["echo", "hello"]})

    assert "error" in result
    assert result.get("error_kind") == "command_not_allowed"
    assert "stdout" not in result


async def test_a_command_outside_the_allowlist_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["rm", "-rf", "/"]})

    assert result.get("error_kind") == "command_not_allowed"
    assert "stdout" not in result


async def test_shell_metacharacters_are_not_interpreted(
    tmp_path: pathlib.Path,
) -> None:
    """No shell. `;` and `&&` must be argv, never operators.

    Running through a shell would make the allowlist meaningless: `echo` is
    allowed, and `echo x; rm -rf /` starts with `echo`.

    The payload is deliberately harmless. An earlier version used
    `rm -rf /tmp`, which meant that under the EXACT regression this test
    exists to catch, running the suite deleted /tmp on the developer's
    machine and in CI before reporting the failure. A safety test whose
    failure mode is destruction is not a safety test. `touch` on a canary
    inside tmp_path proves the same thing: if a shell ran, the file exists.
    """
    canary = tmp_path / "a-shell-ran-here"
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["echo", f"safe; touch {canary} && whoami"]})

    assert result["exit_code"] == 0
    # The metacharacters came back as literal text...
    assert f"safe; touch {canary} && whoami" in result["stdout"]
    # ...and nothing executed them.
    assert not canary.exists(), "a shell interpreted the metacharacters"


async def test_a_string_command_is_refused(tmp_path: pathlib.Path) -> None:
    """`argv` must be a list. A bare string is how a shell sneaks back in."""
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": "echo hello"})

    assert result.get("error_kind") == "invalid_argv"


async def test_the_command_runs_rooted_at_the_policy_root(
    tmp_path: pathlib.Path,
) -> None:
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector(
        {"argv": [sys.executable, "-c", "import os; print(os.getcwd())"]}
    )

    assert str(tmp_path.resolve()) in result["stdout"]


async def test_a_command_that_hangs_is_killed(tmp_path: pathlib.Path) -> None:
    connector = build_terminal_connector(_policy(tmp_path, timeout_s=0.5))

    result = await connector(
        {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
    )

    assert result.get("error_kind") == "timeout"


async def test_output_is_truncated(tmp_path: pathlib.Path) -> None:
    """An unbounded stdout is a denial of service against the model's context."""
    connector = build_terminal_connector(_policy(tmp_path, max_output_bytes=64))

    result = await connector({"argv": [sys.executable, "-c", "print('x' * 10000)"]})

    assert len(result["stdout"]) <= 64
    assert result["truncated"] is True


# --- read_file: rooted, no escapes -----------------------------------------


async def test_reads_a_file_under_the_root(tmp_path: pathlib.Path) -> None:
    (tmp_path / "notes.txt").write_text("hola", encoding="utf-8")
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "notes.txt"})

    assert result["content"] == "hola"


@pytest.mark.parametrize(
    "escape",
    [
        "../outside.txt",
        "sub/../../outside.txt",
        "/etc/passwd",
        "./././../outside.txt",
    ],
)
async def test_paths_outside_the_root_are_refused(
    tmp_path: pathlib.Path, escape: str
) -> None:
    """Traversal, absolute paths and normalisation tricks all refused.

    Checked after resolution, not by string matching: `sub/../../outside.txt`
    contains no leading `..` and still escapes.
    """
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": escape})

    assert result.get("error_kind") == "path_outside_root"
    assert "content" not in result


async def test_a_symlink_pointing_outside_the_root_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """The escape a pure string check cannot see.

    `link.txt` is under the root by every textual measure and resolves
    somewhere else entirely.
    """
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(secret)
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "link.txt"})

    assert result.get("error_kind") == "path_outside_root"
    assert "content" not in result


async def test_a_large_file_is_truncated(tmp_path: pathlib.Path) -> None:
    (tmp_path / "big.txt").write_text("y" * 10000, encoding="utf-8")
    connector = build_file_reader_connector(_policy(tmp_path, max_output_bytes=100))

    result = await connector({"path": "big.txt"})

    assert len(result["content"]) <= 100
    assert result["truncated"] is True


async def test_a_missing_file_is_an_error_not_an_empty_read(
    tmp_path: pathlib.Path,
) -> None:
    """Empty and absent must not look the same to the model."""
    connector = build_file_reader_connector(_policy(tmp_path))

    result = await connector({"path": "nope.txt"})

    assert result.get("error_kind") == "not_found"
    assert "content" not in result


# ---------------------------------------------------------------------------
# The unconfigured default — the property the whole feature is sold on
# ---------------------------------------------------------------------------


async def test_an_unconfigured_deployment_gets_tools_that_refuse_everything(
    tmp_path: pathlib.Path,
) -> None:
    """`build_operator_tool_specs()` with no policy must read NOTHING.

    This had no test at all, and the thing it now pins is a real hole that
    shipped: the previous default supplied `root=Path.cwd()` with an empty
    allowlist. That made `use_term` inert — it consults the allowlist — and
    `read_file` maximally OPEN, because `read_file` never consults the
    allowlist: its only boundary is the root. Under systemd's default
    `WorkingDirectory`, or a container entrypoint that does not chdir, that
    root is `/` and the unconfigured reader served `/etc/passwd` and
    `/proc/self/environ` in one call.

    The test registry registers these specs unconditionally, so this is
    what every deployment gets until it configures a policy.
    """
    from agents_system.connectors.operator import build_operator_tool_specs

    specs = {spec.name: spec for spec in build_operator_tool_specs()}

    term = await specs["use_term"].connector({"argv": ["echo", "hello"]})
    assert term.get("error_kind") == "not_configured"
    assert "stdout" not in term

    # The half that was open. Try the most valuable targets directly.
    for target in (
        "etc/passwd",
        "proc/self/environ",
        "../../../etc/passwd",
        "/etc/passwd",
    ):
        read = await specs["read_file"].connector({"path": target})
        assert read.get("error_kind") == "not_configured", target
        assert "content" not in read, target


async def test_a_null_byte_comes_back_as_a_result_not_an_exception(
    tmp_path: pathlib.Path,
) -> None:
    """`{"argv": ["echo", "a\\u0000b"]}` is legal JSON a model can emit.

    A null byte raises ValueError out of the OS layer. `except OSError` does
    not catch it, the interceptor does not wrap the connector call, and the
    graph catches only TimeoutError and PolicyViolation — so the whole turn
    died and the audit event for the attempt was never written.
    """
    term = build_terminal_connector(_policy(tmp_path))
    reader = build_file_reader_connector(_policy(tmp_path))

    assert (await term({"argv": ["echo", "a\x00b"]}))["error_kind"] == "invalid_input"
    assert (await reader({"path": "ok\x00.txt"}))["error_kind"] == "invalid_input"


async def test_a_large_file_is_never_fully_buffered(tmp_path: pathlib.Path) -> None:
    """The cap must bound MEMORY, not just what is returned.

    `read_bytes()` buffered the whole payload before truncating, so a model
    could exhaust the process by naming a big file inside the root while the
    tool reported `truncated: true`. Measured rather than asserted: reading a
    64 MiB file with a 64-byte cap must not move peak RSS by anything like
    64 MiB.
    """
    import tracemalloc

    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        for _ in range(64):
            handle.write(b"z" * 1024 * 1024)

    connector = build_file_reader_connector(_policy(tmp_path, max_output_bytes=64))

    # tracemalloc, not ru_maxrss. `ru_maxrss` is a monotonic HIGH-WATER MARK
    # for the whole process, so `after - before` is 0 once anything earlier in
    # the same pytest run has already peaked above the file size — the guard
    # goes vacuous as the suite grows heavier, silently, and it is the only
    # proof of this property. tracemalloc measures THIS allocation.
    tracemalloc.start()
    try:
        result = await connector({"path": "big.bin"})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["truncated"] is True
    assert len(result["content"]) <= 64
    assert peak < 8 * 1024 * 1024, (
        f"reading a 64-byte-capped view of a 64 MiB file allocated "
        f"{peak // (1024 * 1024)} MiB"
    )


async def test_a_timed_out_command_leaves_no_surviving_grandchild(
    tmp_path: pathlib.Path,
) -> None:
    """`process.kill()` alone orphans grandchildren, which then outlive the bot.

    The agent got a clean "was killed" refusal that was false about the host,
    and every timed-out call could seed another survivor.

    Found by a MARKER in its own argv, not by the pid it reports: inside the
    sandbox's own PID namespace (ADR-002 C.14's `--unshare-all`) that pid is
    namespace-local and may not name any process at all from the host's
    view -- or worse, could collide with an unrelated one.
    """
    import uuid

    marker = f"sandbox-grandchild-{uuid.uuid4().hex}"
    spawner = tmp_path / "spawn.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        f"    'import time; time.sleep(60)  # {marker}'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    connector = build_terminal_connector(_policy(tmp_path, timeout_s=1.5))
    result = await connector({"argv": [sys.executable, str(spawner)]})

    assert result["error_kind"] == "timeout"

    await asyncio.sleep(0.5)
    survivors = []
    for pid_dir in pathlib.Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode() in cmdline:
            survivors.append(pid_dir.name)

    assert not survivors, f"grandchild(ren) survived the timeout: {survivors}"


async def test_the_child_does_not_inherit_the_parents_secrets(
    tmp_path: pathlib.Path,
) -> None:
    """Every allowlisted command ran holding the bot's own credentials.

    No `env=` meant ANTHROPIC_API_KEY, DATABASE_URL and META_ACCESS_TOKEN
    were handed to any command the allowlist admitted. The inherited PATH
    also decided which binary an allowlisted NAME resolved to, so the
    allowlist matched a string while an environment variable chose the
    program.
    """
    import os

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-must-not-leak"
    try:
        connector = build_terminal_connector(_policy(tmp_path))
        result = await connector(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'ABSENT'));"
                        " print(os.environ.get('PATH'))"
                    ),
                ]
            }
        )
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    assert "sk-ant-must-not-leak" not in result["stdout"]
    assert "ABSENT" in result["stdout"]
    assert "/usr/bin" in result["stdout"], "the child got no usable PATH"


# ---------------------------------------------------------------------------
# Output that arrives in more than one flush
# ---------------------------------------------------------------------------


async def test_output_arriving_in_several_flushes_comes_back_whole(
    tmp_path: pathlib.Path,
) -> None:
    """`truncated: false` must mean the output IS complete.

    `StreamReader.read(n)` returns what is currently buffered, not n bytes
    and not up to EOF, so a single call captured only the first flush. A
    real measurement: `find . -type f` came back with 245 of 2400 lines and
    `truncated: false` — and this tool's own description tells the model
    "never state that a file ends where a truncated read ends", so a false
    `truncated` is the one lie it is instructed to believe.

    The cap here is far above the payload, so nothing should be cut.
    """
    connector = build_terminal_connector(_policy(tmp_path, max_output_bytes=1_000_000))

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-u",
                "-c",
                (
                    "import sys, time\n"
                    "for i in range(20):\n"
                    "    sys.stdout.write(f'line-{i}\\n'); sys.stdout.flush()\n"
                    "    time.sleep(0.01)\n"
                ),
            ]
        }
    )

    assert result["exit_code"] == 0
    assert result["truncated"] is False
    assert result["stdout"].count("line-") == 20, (
        f"only {result['stdout'].count('line-')} of 20 flushes survived"
    )


async def test_output_larger_than_a_pipe_buffer_does_not_time_out(
    tmp_path: pathlib.Path,
) -> None:
    """Stopping at the cap left the child blocked on a full pipe.

    Once the reader had its bytes it stopped, the OS pipe filled, the child
    blocked on write forever and `process.wait()` never returned — so the
    wall-clock timeout fired and SIGKILLed a command that was working fine,
    losing the output AND the exit code. Measured threshold: ~256-300 KB.

    A truncated result is the correct answer here; a timeout is not.
    """
    connector = build_terminal_connector(
        _policy(tmp_path, max_output_bytes=8192, timeout_s=5.0)
    )

    result = await connector({"argv": [sys.executable, "-c", "print('z' * 2_000_000)"]})

    assert result.get("error_kind") != "timeout", (
        "a working command was killed because the reader stopped draining"
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) <= 8192
    assert result["truncated"] is True


async def test_a_large_stderr_does_not_stall_the_stdout_read(
    tmp_path: pathlib.Path,
) -> None:
    """The cross-stream case, which is why `communicate()` exists."""
    connector = build_terminal_connector(
        _policy(tmp_path, max_output_bytes=4096, timeout_s=5.0)
    )

    result = await connector(
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('e' * 1_000_000); print('done')",
            ]
        }
    )

    assert result.get("error_kind") != "timeout"
    assert "done" in result["stdout"]
    assert result["truncated"] is True


async def test_a_deployment_can_actually_configure_the_sandbox(
    tmp_path: pathlib.Path,
) -> None:
    """The documented escape hatch, which did not exist.

    The registry registers the refusing specs unconditionally,
    `ToolRegistry.register` rejects duplicates, and there is no unregister —
    so "an application that wants terminal access registers these specs
    itself" raised `ValueError: Tool already registered: use_term`. The tools
    were permanently `not_configured` and could not be enabled by the method
    their own docstring named.
    """
    from conftest import build_test_registry

    (tmp_path / "hello.txt").write_text("configured", encoding="utf-8")
    policy = TerminalPolicy(
        root=tmp_path,
        allowed_commands=frozenset({"echo"}),
        sandbox=SandboxPolicy(),
        timeout_s=5.0,
    )

    registry = build_test_registry(terminal_policy=policy)

    reader = registry.get("read_file").connector
    assert (await reader({"path": "hello.txt"}))["content"] == "configured"

    term = registry.get("use_term").connector
    assert (await term({"argv": ["echo", "ok"]}))["exit_code"] == 0
    # And the allowlist still binds.
    assert (await term({"argv": ["rm", "-rf", "/"]}))["error_kind"] == (
        "command_not_allowed"
    )


async def test_an_unreadable_or_overlong_path_is_a_result_not_an_exception(
    tmp_path: pathlib.Path,
) -> None:
    """`Path.is_file()` lets ENAMETOOLONG and EACCES through.

    It swallows only ENOENT/ENOTDIR/EBADF/ELOOP. A 5000-character path and an
    unreadable directory component inside the root both escaped the connector
    as exceptions — the exact failure the null-byte guard two lines above was
    added to prevent.
    """
    import os

    connector = build_file_reader_connector(_policy(tmp_path))

    long_path = await connector({"path": "a" * 5000})
    assert long_path.get("error_kind") in {"invalid_path", "not_found"}
    assert "content" not in long_path

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("s", encoding="utf-8")
    os.chmod(locked, 0o000)
    try:
        denied = await connector({"path": "locked/secret.txt"})
    finally:
        os.chmod(locked, 0o755)

    assert "error_kind" in denied
    assert "content" not in denied


# ---------------------------------------------------------------------------
# ADR-002 C.14 — the bubblewrap sandbox: required, and fails closed
# ---------------------------------------------------------------------------


def test_terminal_policy_requires_a_sandbox_field(tmp_path: pathlib.Path) -> None:
    """`sandbox` has no default -- the same "no permissive default" posture
    `root` and `allowed_commands` already have. A deployment that forgets to
    pass one gets a `TypeError` at construction, not a policy that quietly
    runs unsandboxed.
    """
    with pytest.raises(TypeError):
        TerminalPolicy(  # type: ignore[call-arg]
            root=tmp_path, allowed_commands=frozenset({"echo"})
        )


async def test_use_term_refuses_when_bwrap_is_unavailable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `bwrap` on the host -> refuse. Never fall back to running raw.

    Simulates absence by making the module's own lookup return None, rather
    than fighting with `$PATH`, so the test is about the refusal, not about
    hiding a real binary.
    """
    import agents_system.connectors.operator as operator_module

    monkeypatch.setattr(operator_module, "_bwrap_path", lambda: None)
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["echo", "hello"]})

    assert result.get("error_kind") == "sandbox_unavailable"
    assert "stdout" not in result


def test_bwrap_argv_clears_the_environment_before_setting_it_explicitly(
    tmp_path: pathlib.Path,
) -> None:
    """`--clearenv` before `--setenv` -- defense in depth alongside the
    outer `env=_child_env(policy)` already passed to `create_subprocess_exec`
    (which alone already keeps the HOST's broader environment away from
    `bwrap`'s own process): if a future change ever widens that outer `env=`,
    this inner guard still stops it from reaching the sandboxed command.
    """
    from agents_system.connectors.operator import _bwrap_argv

    policy = _policy(tmp_path)
    wrapped = _bwrap_argv(["echo", "hi"], policy=policy, bwrap="/usr/bin/bwrap")

    assert "--clearenv" in wrapped
    clear_index = wrapped.index("--clearenv")
    setenv_indices = [i for i, tok in enumerate(wrapped) if tok == "--setenv"]
    assert setenv_indices, "no --setenv for _child_env's PATH/HOME/LANG/LC_ALL"
    assert all(i > clear_index for i in setenv_indices), (
        "--setenv appeared before --clearenv"
    )


# ---------------------------------------------------------------------------
# ADR-002 C.14 review follow-up (PR #148) — forbidden roots, prlimit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/root"),
        pathlib.Path("/run"),
        pathlib.Path("/var/run"),
    ],
)
def test_terminal_policy_rejects_a_forbidden_root(forbidden: pathlib.Path) -> None:
    """`root` is bound READ-WRITE inside the sandbox (ADR-002 C.14) -- the
    filesystem root, `/home`, `/root`, `/run` and `/var/run` are refused
    outright, the same review follow-up (PR #148) that closed the
    `command_tools` `Path.cwd()` hole."""
    with pytest.raises(ValueError, match="forbids"):
        TerminalPolicy(
            root=forbidden,
            allowed_commands=frozenset({"echo"}),
            sandbox=SandboxPolicy(),
        )


def test_terminal_policy_rejects_home_as_root() -> None:
    with pytest.raises(ValueError, match="forbids"):
        TerminalPolicy(
            root=pathlib.Path.home(),
            allowed_commands=frozenset({"echo"}),
            sandbox=SandboxPolicy(),
        )


def test_terminal_policy_rejects_the_process_cwd_as_root() -> None:
    """The exact shape of the pre-follow-up `command_tools` bug: a policy
    rooted at wherever the process happens to be running from -- typically
    the repository checkout -- gets refused at construction, independent of
    `command_tools`'s own fix (its per-call scratch workspace)."""
    with pytest.raises(ValueError, match="forbids"):
        TerminalPolicy(
            root=pathlib.Path.cwd(),
            allowed_commands=frozenset({"echo"}),
            sandbox=SandboxPolicy(),
        )


@pytest.mark.parametrize(
    "forbidden",
    [
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/root"),
        pathlib.Path("/run"),
        pathlib.Path("/var/run"),
    ],
)
def test_sandbox_policy_rejects_a_forbidden_extra_ro_bind(
    forbidden: pathlib.Path,
) -> None:
    with pytest.raises(ValueError, match="forbids"):
        SandboxPolicy(extra_ro_binds=(forbidden,))


def test_sandbox_policy_rejects_home_as_an_extra_ro_bind() -> None:
    with pytest.raises(ValueError, match="forbids"):
        SandboxPolicy(extra_ro_binds=(pathlib.Path.home(),))


def test_sandbox_policy_accepts_a_narrow_extra_ro_bind(tmp_path: pathlib.Path) -> None:
    """The guard is about the specific forbidden list, not about being
    somewhere under one of those trees -- a narrow, purpose-built directory
    is exactly what `extra_ro_binds` is FOR."""
    narrow = tmp_path / "some-tool-install"
    narrow.mkdir()
    SandboxPolicy(extra_ro_binds=(narrow,))  # must not raise


async def test_use_term_refuses_when_prlimit_is_unavailable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `prlimit` on the host -> refuse, same fail-closed posture as a
    missing `bwrap` -- running sandboxed but with NO enforced memory/CPU
    ceiling is not an acceptable degraded mode (ADR-002 C.14)."""
    import agents_system.connectors.operator as operator_module

    monkeypatch.setattr(operator_module, "_prlimit_path", lambda: None)
    connector = build_terminal_connector(_policy(tmp_path))

    result = await connector({"argv": ["echo", "hello"]})

    assert result.get("error_kind") == "sandbox_unavailable"
    assert "stdout" not in result


def test_run_argv_wraps_bwrap_in_prlimit_not_preexec_fn() -> None:
    """Structural proof that resource limits are applied by `prlimit`
    (a separate program) rather than `preexec_fn` (a fork-time callback in
    THIS process, which Python's docs call unsafe in the presence of
    threads) -- PR #148 review follow-up."""
    import inspect

    import agents_system.connectors.operator as operator_module

    assert not hasattr(operator_module, "_rlimits"), (
        "the preexec_fn-based _rlimits helper should be gone entirely"
    )
    source = inspect.getsource(operator_module._run_argv)
    assert "preexec_fn" not in source

    wrapped = operator_module._prlimit_argv(
        ["/usr/bin/bwrap", "--unshare-all"],
        policy=_policy(pathlib.Path("/tmp")),
        prlimit="/usr/bin/prlimit",
    )
    assert wrapped[0] == "/usr/bin/prlimit"
    assert any(tok.startswith("--as=") for tok in wrapped)
    assert any(tok.startswith("--cpu=") for tok in wrapped)
    assert wrapped[wrapped.index("--") + 1] == "/usr/bin/bwrap"
