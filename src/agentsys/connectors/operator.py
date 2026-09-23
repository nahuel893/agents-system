"""The two operator tools: running a command, and reading a file.

These are the platform's most dangerous connectors by a wide margin.
`use_term` is arbitrary code execution on the host and `read_file` is
arbitrary data exfiltration from it, so the design question is not "how do we
make these convenient" but "what does an agent have to be given before either
one does anything at all".

Three properties, in order of importance:

**They fail closed.** `TerminalPolicy` has no default root and no default
allowlist. An empty allowlist runs nothing. A deployment that declares
`use_term` and forgets to configure it gets a tool that refuses every call,
which is the only safe way to be wrong.

**There is no shell.** Commands run through `create_subprocess_exec`, never
`create_subprocess_shell`. A shell would make the allowlist decorative: `echo`
is allowed, and `echo x; rm -rf /` starts with `echo`. Metacharacters arrive
at the process as literal argv and nothing interprets them.

**Paths are checked after resolution.** `sub/../../etc/passwd` contains no
leading `..`, and a symlink under the root can point anywhere. Both are caught
because the check is `resolved.is_relative_to(root.resolve())`, not a string
comparison.

Everything an agent gets back is a RESULT, never an exception: a refusal is
something the model can reason about and report, while a raised error escapes
the tool boundary and degrades differently at every entry point.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import shutil
import signal
import threading
from typing import Any, Awaitable, Callable

import structlog

from agentsys.harness.registry import Tier, ToolSpec

_logger = structlog.get_logger()

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]


def _forbidden_sandbox_paths() -> frozenset[pathlib.Path]:
    """Paths a sandbox-related field must never resolve to (ADR-002 C.14
    review follow-up, PR #148).

    Best effort, not exhaustive: `Path.home()` covers `$HOME`, and
    `Path.cwd()` covers the exact mistake that shipped in `command_tools`
    before this follow-up -- a policy rooted at the PROCESS's own working
    directory, mounted read-write, let a sandboxed command read and
    overwrite the real repository's `.env` and `.git/config`. Computed
    fresh on every call (never memoised at import time), since `Path.cwd()`
    can change between one caller and the next.
    """
    candidates = (
        pathlib.Path("/"),
        pathlib.Path("/home"),
        pathlib.Path("/root"),
        pathlib.Path("/run"),
        pathlib.Path("/var/run"),
        pathlib.Path.home(),
        pathlib.Path.cwd(),
    )
    return frozenset(candidate.resolve() for candidate in candidates)


@dataclasses.dataclass(frozen=True)
class SandboxPolicy:
    """The bubblewrap (`bwrap`) sandbox a command runs inside (ADR-002 C.14).

    `TerminalPolicy.sandbox` has no default -- a command with no sandbox
    policy cannot run at all, the same "no permissive default" posture
    `root` and `allowed_commands` already have. This dataclass's OWN fields
    may default, because every default here is the CLOSED one: no network,
    no extra binds beyond the fixed read-only system paths and whatever
    directory `argv[0]` itself resolves under (bound automatically -- see
    `_bwrap_argv`), and a conservative memory ceiling.
    """

    network: bool = False
    """Whether the sandboxed command may reach the network. `False` unless
    a tool explicitly declares it needs one."""

    extra_ro_binds: tuple[pathlib.Path, ...] = ()
    """Additional read-only bind mounts a command needs beyond the fixed
    system paths (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`) and its
    own resolved install directory. Empty by default: a deployment states
    exactly what an allowlisted command needs, rather than the sandbox
    guessing."""

    max_memory_bytes: int = 512 * 1024 * 1024
    """`RLIMIT_AS` ceiling for the sandboxed process and everything it
    forks, enforced via `prlimit` -- bubblewrap itself has no resource-limit
    flags, only namespace/mount isolation."""

    def __post_init__(self) -> None:
        for path in self.extra_ro_binds:
            resolved = path.resolve()
            if resolved in _forbidden_sandbox_paths():
                raise ValueError(
                    f"SandboxPolicy.extra_ro_binds contains '{path}', which "
                    f"resolves to '{resolved}' -- ADR-002 C.14 forbids binding "
                    "the filesystem root, /home, $HOME, /root, /run or "
                    "/var/run into a sandbox, even read-only, since any of "
                    "those exposes every user's files (review follow-up on "
                    "PR #148: a widened bind list this broad is exactly how "
                    "the pre-fix `command_tools` workspace leaked .env)."
                )


@dataclasses.dataclass(frozen=True)
class TerminalPolicy:
    """What an operator agent is allowed to do on the host.

    `root`, `allowed_commands` and `sandbox` are REQUIRED and have no
    defaults. A default root would be whatever directory the process
    happened to start in, a default allowlist would be a guess about which
    commands are safe, and a default sandbox would be a guess about what a
    command needs beyond it -- none of these is a decision this library can
    make for a deployment it has never seen.
    """

    root: pathlib.Path
    """Working directory for every command, and the ceiling for every read.

    Must be a DEDICATED, scoped-down workspace -- never the process's own
    working directory, `$HOME`, `/root`, or the filesystem root. `root` is
    bound READ-WRITE inside the sandbox at the same path (ADR-002 C.14), so
    pointing it at an ambient directory (e.g. a deployment's own repository
    checkout) hands a sandboxed command read-write access to everything in
    it -- exactly the shape of the pre-follow-up `command_tools` bug this
    check exists to catch (PR #148 review): it hardcoded `root=Path.cwd()`,
    so a sandboxed command tool could read and overwrite the real `.env`
    and `.git/config`."""

    allowed_commands: frozenset[str]
    """Exact `argv[0]` values that may run. Empty means nothing runs."""

    sandbox: SandboxPolicy
    """The bubblewrap sandbox every command runs inside (ADR-002 C.14). No
    default: a `TerminalPolicy` with no sandbox policy cannot be constructed,
    let alone run one unsandboxed."""

    timeout_s: float = 10.0
    """Wall clock per command. A hung command is killed, not awaited."""

    max_output_bytes: int = 8192
    """Cap on returned stdout and file content.

    Unbounded output is a denial of service against the model's context
    window, and a large read is also the shape data exfiltration takes.
    """

    def __post_init__(self) -> None:
        resolved = self.root.resolve()
        if resolved in _forbidden_sandbox_paths():
            raise ValueError(
                f"TerminalPolicy.root is '{self.root}', which resolves to "
                f"'{resolved}' -- ADR-002 C.14 forbids the filesystem root, "
                "/home, $HOME, /root, /run, /var/run, or the process's own "
                "working directory as a sandbox root, because it is bound "
                "READ-WRITE inside the sandbox. root must be a dedicated "
                "workspace created for this purpose."
            )


def _refuse(kind: str, message: str, **fields: Any) -> ConnectorOutput:
    _logger.warning(f"operator.{kind}", **fields)
    return {"error": message, "error_kind": kind}


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), truncated


def _reject_null_bytes(values: list[str]) -> ConnectorOutput | None:
    """A null byte anywhere raises ValueError out of the OS layer.

    `json.loads('{"argv": ["echo", "a\\u0000b"]}')` is legal JSON, so a model
    tool call carries one through unchanged. `except OSError` does not catch
    ValueError, the interceptor does not wrap the connector call, and the
    graph catches only TimeoutError and PolicyViolation -- so the whole turn
    dies and the audit event for the attempt is never emitted. Everything an
    agent gets back must be a RESULT.
    """
    if any("\x00" in v for v in values):
        return _refuse("invalid_input", "a null byte is not a valid argument.")
    return None


def _child_env(policy: TerminalPolicy) -> dict[str, str]:
    """A minimal environment, because the parent's is full of credentials.

    Passing no `env=` hands every allowlisted command the bot's own
    ANTHROPIC_API_KEY, DATABASE_URL and META_ACCESS_TOKEN. It also lets the
    inherited PATH decide which binary an allowlisted NAME resolves to, so
    the allowlist would match a string while an environment variable chose
    the program. A fixed PATH makes the name mean one thing.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(policy.root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _bwrap_path() -> str | None:
    """Absolute path to `bwrap`, or None if it is not on `$PATH`.

    Checked fresh on every call through `_run_argv` -- the one shared seam
    both `use_term` and `command_tools` execute through -- rather than
    cached, so a test can simulate absence (monkeypatching this function) or
    a host that loses its `bwrap` install mid-process is caught on the very
    next command, never serving stale "sandbox available" state.
    """
    return shutil.which("bwrap")


#: Read-only system paths bound into every sandbox, tried in order and
#: silently skipped when absent (`--ro-bind-try`) -- portable across a
#: merged-`/usr` layout (where these are symlinks into `/usr`) and a
#: traditional split one.
#:
#: `/etc` is bound WHOLE rather than an allowlist of specific files (review
#: follow-up, PR #148): it is read-only, contains no per-user secrets (those
#: live under `/home`/`/root`, already excluded -- see
#: `_forbidden_sandbox_paths`), and an allowlist would have to be kept in
#: sync with whatever files an arbitrary allowlisted command happens to
#: need (locale data, `nsswitch.conf`, terminfo, CA bundles for a
#: network-declared tool, ...) with no reliable way to enumerate that set
#: up front. The narrower, auditable boundary here is WHICH commands can
#: run at all (`allowed_commands`/`command_tools` declarations) and WHAT
#: network/extra-bind access each one gets (`SandboxPolicy`), not
#: micromanaging one read-only system directory.
_SYSTEM_RO_BINDS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

_MAX_SYMLINK_HOPS = 80


def _symlink_hops(path: pathlib.Path) -> list[pathlib.Path]:
    """Walk *path* component by component the way the kernel does, and
    return every path touched along the way.

    `Path.resolve()` collapses a whole symlink chain into just the final
    target and silently loses every directory an INTERMEDIATE hop passed
    through. A venv's interpreter is commonly a short chain through SEVERAL
    directories -- its own `bin/python3 -> python` (a sibling), then
    `python -> /some/toolchain/.../pythonX.Y` (elsewhere entirely), where a
    directory partway through THAT target is itself a version-alias symlink
    to a sibling real directory -- and every hop independently has to exist
    inside the sandbox for `execvp` to walk the whole chain, not only the
    fully-resolved final file. Bounded iteration guards a symlink cycle.
    """
    hops: list[pathlib.Path] = []
    current = pathlib.Path("/")
    pending = list(path.parts[1:]) if path.is_absolute() else list(path.parts)
    for _ in range(_MAX_SYMLINK_HOPS):
        if not pending:
            break
        current = current / pending.pop(0)
        hops.append(current)
        if current.is_symlink():
            target = pathlib.Path(os.readlink(current))
            if target.is_absolute():
                current = pathlib.Path("/")
                pending = list(target.parts[1:]) + pending
            else:
                current = current.parent
                pending = list(target.parts) + pending
    return hops


def _program_ro_binds(program: str) -> list[str]:
    """Read-only bind roots needed to `execvp` *program* (ADR-002 C.14).

    Only the directory of each SYMLINK hop, plus the final real file's own
    directory -- never every ancestor along the way. `bwrap` auto-creates
    empty scaffold directories for the rest of a bind destination's path
    prefix, so binding e.g. `/home/nh/.local/share/uv/python` does not also
    expose the REST of `/home/nh` -- only that one path gets real content.
    `program` was already vetted by the caller (an allowlist entry, or a
    `command_tools` declaration resolved to an absolute path at load time),
    so making its own install tree readable does not expand what can run.
    """
    if not program.startswith("/"):
        return []  # A bare name relies on the fixed system paths instead.

    hops = _symlink_hops(pathlib.Path(program))
    roots = {str(hop.parent) for hop in hops if hop.is_symlink()}
    if hops:
        roots.add(str(hops[-1].parent))
    roots.discard("/")
    return sorted(roots)


def _bwrap_argv(argv: list[str], *, policy: TerminalPolicy, bwrap: str) -> list[str]:
    """Wrap *argv* in a `bwrap` invocation enforcing *policy.sandbox*
    (ADR-002 C.14).

    `--unshare-all` unshares every namespace bubblewrap supports (user, ipc,
    pid, net, uts, cgroup) -- the maximally closed starting point -- and
    `--share-net` opts back INTO network access only when the policy
    explicitly declares it. The root is bound read-write at the SAME path
    inside the sandbox as outside, so `policy.root`-relative behaviour
    (`cwd`, `read_file`'s own root check) is unchanged by sandboxing.

    Whatever `argv[0]` needs to `execvp` is bound read-only automatically
    (see `_program_ro_binds`) -- it was already vetted by the caller (the
    allowlist, or a `command_tools` declaration resolved at load time), so
    making it executable does not expand what can run. A bare (non-absolute)
    `argv[0]` relies on the fixed system paths above instead.
    """
    sandbox = policy.sandbox
    root = str(policy.root)
    wrapped = [bwrap, "--unshare-all", "--die-with-parent", "--new-session"]
    if sandbox.network:
        wrapped.append("--share-net")

    for path in _SYSTEM_RO_BINDS:
        wrapped += ["--ro-bind-try", path, path]

    for program_root in _program_ro_binds(argv[0]):
        wrapped += ["--ro-bind-try", program_root, program_root]
    for extra in sandbox.extra_ro_binds:
        wrapped += ["--ro-bind-try", str(extra), str(extra)]

    wrapped += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        root,
        root,
        "--chdir",
        root,
        "--clearenv",
    ]
    for key, value in _child_env(policy).items():
        wrapped += ["--setenv", key, value]
    wrapped.append("--")
    wrapped += argv
    return wrapped


def _prlimit_path() -> str | None:
    """Absolute path to `prlimit`, or None if it is not on `$PATH`.

    Checked fresh on every call, exactly like `_bwrap_path` (ADR-002 C.14
    review follow-up, PR #148): an earlier version applied `RLIMIT_AS`/
    `RLIMIT_CPU` via `preexec_fn` on the `bwrap`-spawning
    `create_subprocess_exec` call. Python's own docs call `preexec_fn`
    unsafe in the presence of threads: it runs in the child between
    `fork()` and `exec()`, and if any OTHER thread in this (asyncio, and in
    production, uvicorn-hosted) process held a lock at the moment of the
    fork -- the logging module's own lock is the textbook example -- the
    child can deadlock forever holding a lock nothing will ever release.
    `prlimit COMMAND` sidesteps this entirely: it is a normal, separate,
    already-compiled program (util-linux, present on every mainstream Linux
    distribution, including GitHub's `ubuntu-latest` runners), so applying
    it is just another `execvp`, never a fork from inside this process.
    """
    return shutil.which("prlimit")


def _prlimit_argv(wrapped: list[str], *, policy: TerminalPolicy, prlimit: str) -> list[str]:
    """Prepend *wrapped* (an already-built `bwrap` invocation) with
    `prlimit --as=... --cpu=... --` (ADR-002 C.14).

    `prlimit` sets the limits on ITSELF before `exec`-ing `wrapped`, and
    `RLIMIT_AS`/`RLIMIT_CPU` are inherited across exec, so they bound
    bubblewrap's own (tiny) setup footprint AND everything it goes on to
    fork or exec inside the sandbox, with no separate cgroup or daemon --
    the same coverage `preexec_fn` gave, without forking this process.
    `RLIMIT_CPU` (CPU time) tracks *policy.timeout_s* with headroom, so it
    is a backstop against a busy loop, never a race with the primary
    wall-clock timeout in `_run_argv` (which alone governs an idle/blocked
    command: `time.sleep()` burns no CPU time at all).
    """
    memory = policy.sandbox.max_memory_bytes
    cpu_seconds = max(int(policy.timeout_s) + 5, 5)
    return [prlimit, f"--as={memory}", f"--cpu={cpu_seconds}", "--", *wrapped]


def log_sandbox_availability_at_boot(*, context: str) -> None:
    """Log once, at connector-construction time, if the sandbox cannot run
    (ADR-002 C.14 review follow-up, PR #148).

    Called from `build_terminal_connector` and `command_tools.
    build_command_tool_connector` -- both run at BOOT (runtimes are built
    once at startup, per `main.py`'s `lifespan`), not per call, so this is
    the boot-time signal the follow-up asked for: an operator watching
    startup logs sees `operator.sandbox_unavailable_at_boot` immediately,
    instead of only discovering it from a `sandbox_unavailable` refusal the
    first time an agent actually tries to use the tool. Deliberately does
    NOT raise or otherwise stop the deployment from booting -- an unrelated
    role should not go down because one role's operator/command tools are
    misconfigured; `_run_argv` still refuses every call regardless of
    whether this warning was ever read.
    """
    missing = [
        name for name, path in (("bwrap", _bwrap_path()), ("prlimit", _prlimit_path()))
        if path is None
    ]
    if missing:
        _logger.error(
            "operator.sandbox_unavailable_at_boot",
            context=context,
            missing=missing,
            detail=(
                f"{' and '.join(missing)} not found on $PATH; every "
                "sandboxed command will refuse at call time with "
                "error_kind='sandbox_unavailable' (ADR-002 C.14). See "
                "docs/operations/sandbox-bwrap.md."
            ),
        )


_STREAM_CHUNK = 64 * 1024


async def _read_capped(stream: Any, limit: int) -> tuple[bytes, bool]:
    """Read to EOF, keeping at most `limit` bytes and discarding the rest.

    Two things this must do at once, and an earlier version did neither.

    **Read to EOF.** `StreamReader.read(n)` returns whatever is CURRENTLY
    buffered -- not n bytes, and not up to EOF. A single call therefore
    captured only the first flush: `find . -type f` came back with 245 of
    2400 lines and `truncated: false`, which is the field this tool's own
    description tells the model to trust.

    **Keep draining past the cap.** A reader that stops once it has enough
    leaves the pipe full, so the child blocks on write forever and
    `process.wait()` never returns. The wall-clock timeout then fired and
    SIGKILLed a command that was working: `cat` on a 1.2 MB log inside the
    root took 3 s and returned a timeout instead of a truncated result.

    Memory stays bounded because only `limit` bytes are KEPT; everything
    past it is read and dropped one chunk at a time. `communicate()`
    achieved both and buffered without bound; this achieves both without.
    """
    if stream is None:
        return b"", False

    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(_STREAM_CHUNK)
        if not chunk:
            break
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > max(room, 0):
            truncated = True
    return bytes(kept), truncated


async def _run_argv(argv: list[str], *, policy: TerminalPolicy) -> ConnectorOutput:
    """Spawn *argv* under *policy*, sandboxed, with no shell — the shared
    execution seam.

    Extracted out of `use_term` (ADR-002 C.12) so `connectors.command_tools`
    reuses the exact same `create_subprocess_exec` + timeout + output-cap
    machinery instead of a second command runner, and so ADR-002 C.14's
    bubblewrap sandbox has exactly ONE seam to wrap: whatever runs `argv`
    under `policy` for either caller runs through here, inside `bwrap`.

    Callers are responsible for everything upstream of "this argv is safe to
    spawn" — allowlist/typed-param validation, null-byte rejection. This
    function only spawns (sandboxed), drains bounded output, and enforces
    the timeout.

    Fails closed: if `bwrap` OR `prlimit` is not on the host, this refuses
    and never falls back to running *argv* unsandboxed, or sandboxed but
    without enforced resource limits (ADR-002 C.14).
    """
    program = argv[0]
    bwrap = _bwrap_path()
    if bwrap is None:
        return _refuse(
            "sandbox_unavailable",
            "bubblewrap ('bwrap') is not installed on this host; refusing "
            f"to run '{program}' unsandboxed (ADR-002 C.14).",
            program=program,
        )
    prlimit = _prlimit_path()
    if prlimit is None:
        return _refuse(
            "sandbox_unavailable",
            "'prlimit' (util-linux) is not installed on this host; refusing "
            f"to run '{program}' without enforced memory/CPU limits "
            "(ADR-002 C.14).",
            program=program,
        )

    sandboxed_argv = _prlimit_argv(
        _bwrap_argv(argv, policy=policy, bwrap=bwrap), policy=policy, prlimit=prlimit
    )

    # Spawn AND drain both live inside one `asyncio.timeout` block, not just
    # the drain: an earlier version left `create_subprocess_exec` outside
    # the timeout entirely (ADR-002 C.14 review follow-up, PR #148), so a
    # spawn that never returned would hang the call forever. `process`
    # stays a plain local -- no nested coroutine, no outer-scope list -- so
    # there is nothing extra keeping the `Process`/transport alive past its
    # natural scope for the cyclic GC to collect later, possibly after a
    # LATER call's event loop has replaced this one (that WAS observed as a
    # spurious "Event loop is closed" warning from an earlier, more
    # convoluted version of this function during this same review round).
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(policy.timeout_s):
            process = await asyncio.create_subprocess_exec(
                *sandboxed_argv,
                cwd=str(policy.root),
                env=_child_env(policy),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Its own process group, so a timeout can kill the whole
                # tree (prlimit, bwrap, and everything bwrap sandboxes)
                # rather than only the direct child.
                start_new_session=True,
            )
            (stdout_bytes, out_cut), (stderr_bytes, err_cut) = await asyncio.gather(
                _read_capped(process.stdout, policy.max_output_bytes),
                _read_capped(process.stderr, policy.max_output_bytes),
            )
            await process.wait()
    except TimeoutError:
        # `TimeoutError` is an `OSError` subclass, so this branch MUST be
        # checked before the `except OSError` below -- listing them in the
        # other order would silently swallow every real timeout as a
        # "spawn_failed" instead.
        if process is not None:
            _kill_group(process)
        return _refuse(
            "timeout",
            f"'{program}' exceeded {policy.timeout_s}s and was killed.",
            program=program,
        )
    except (OSError, ValueError) as error:
        return _refuse(
            "spawn_failed",
            f"could not start '{program}': {error}",
            program=program,
        )

    assert process is not None  # reached only past a successful spawn above
    return {
        "exit_code": process.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "truncated": out_cut or err_cut,
    }


def build_terminal_connector(policy: TerminalPolicy) -> AsyncConnector:
    """Build `use_term` over *policy*."""
    log_sandbox_availability_at_boot(context="use_term")

    async def use_term(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        argv = inputs.get("argv")

        # A string here is how a shell sneaks back in: the caller means
        # "interpret this", and the only way to honour it is to invoke one.
        if not isinstance(argv, list) or not argv:
            return _refuse(
                "invalid_argv",
                "argv must be a non-empty list of strings, not a command line. "
                "There is no shell: pass ['ls', '-la'], never 'ls -la'.",
            )
        if not all(isinstance(part, str) for part in argv):
            return _refuse("invalid_argv", "every element of argv must be a string.")
        rejected = _reject_null_bytes(argv)
        if rejected is not None:
            return rejected

        program = argv[0]
        if program not in policy.allowed_commands:
            allowed = ", ".join(sorted(policy.allowed_commands)) or "(none)"
            return _refuse(
                "command_not_allowed",
                f"'{program}' is not in this deployment's allowlist. "
                f"Allowed: {allowed}.",
                program=program,
            )

        return await _run_argv(argv, policy=policy)

    return use_term


def _kill_group(process: Any) -> None:
    """Kill the child's whole process group, not just the child.

    `process.kill()` alone leaves grandchildren orphaned and running
    indefinitely, so the agent gets a clean "was killed" refusal that is
    false about the host. Each timed-out call could seed another survivor.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already gone, or we cannot signal the group; fall back to the child.
        try:
            process.kill()
        except ProcessLookupError:
            pass


def build_file_reader_connector(policy: TerminalPolicy) -> AsyncConnector:
    """Build `read_file` over *policy*."""

    root = policy.root.resolve()

    def _read_bounded(path: pathlib.Path, limit: int) -> tuple[bytes, bool]:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        return data[:limit], len(data) > limit

    async def read_file(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        raw_path = inputs.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _refuse("invalid_path", "path must be a non-empty string.")
        rejected = _reject_null_bytes([raw_path])
        if rejected is not None:
            return rejected

        # Resolve BEFORE deciding. `sub/../../etc/passwd` has no leading `..`,
        # and a symlink under the root can point anywhere on the host; a string
        # check sees neither.
        try:
            candidate = (root / raw_path).resolve()
        except (OSError, ValueError) as error:
            return _refuse(
                "invalid_path", f"could not resolve '{raw_path}': {error}"
            )

        if not candidate.is_relative_to(root):
            return _refuse(
                "path_outside_root",
                "path resolves outside this deployment's root.",
                requested=raw_path,
            )

        try:
            is_file = candidate.is_file()
        except OSError as error:
            # `Path.is_file()` swallows ENOENT/ENOTDIR/EBADF/ELOOP and lets
            # everything else through: a 5000-character path raises
            # ENAMETOOLONG, and an unreadable directory component inside the
            # root raises EACCES. Both are trivially model-emittable, and
            # both escaped the connector as exceptions -- the exact failure
            # the null-byte guard above was added to prevent, one line down.
            return _refuse(
                "invalid_path",
                f"could not stat '{raw_path}': {error}",
                requested=raw_path,
            )

        if not is_file:
            return _refuse(
                "not_found",
                f"no file at '{raw_path}'.",
                requested=raw_path,
            )

        try:
            # Off the event loop AND bounded. Reading synchronously here gave
            # the graph's per-tool `asyncio.timeout` no suspension point to
            # cancel at, so one large read stalled every other session in the
            # process; reading it whole let a model exhaust memory by naming
            # a big file, while the tool still reported `truncated: true`.
            data, truncated = await asyncio.to_thread(
                _read_bounded, candidate, policy.max_output_bytes
            )
        except OSError as error:
            return _refuse(
                "read_failed",
                f"could not read '{raw_path}': {error}",
                requested=raw_path,
            )

        return {
            "content": data.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    return read_file


def _unconfigured_connector(tool: str) -> AsyncConnector:
    """A connector that refuses everything, for a tool with no policy.

    This replaces an earlier `INERT_POLICY` that set `root=Path.cwd()` and an
    empty allowlist. That made `use_term` inert -- it consults the allowlist
    -- and made `read_file` maximally OPEN, because `read_file` never
    consults the allowlist at all: its only boundary is the root, and the
    root was wherever the process happened to start. Under systemd's default
    `WorkingDirectory`, or a container entrypoint that does not chdir, that
    root is `/` and the unconfigured reader serves the entire host
    filesystem -- `/etc/passwd`, `/proc/self/environ`, every secret in the
    bot's own environment -- with no traversal and no race.

    "Fails closed" was true of the dataclass and false of the object that
    shipped. A tool with no policy now has no working connector at all,
    which is the only shape where that sentence is true of both.
    """

    async def refuse(inputs: dict[str, Any], *, session: Any = None) -> ConnectorOutput:
        return _refuse(
            "not_configured",
            f"'{tool}' is registered but this deployment supplied no "
            f"TerminalPolicy, so it has no root and no allowlist. It refuses "
            f"every call until one is configured.",
            tool=tool,
        )

    return refuse


_UNCONFIGURED_SPECS: tuple[ToolSpec, ...] | None = None
_UNCONFIGURED_LOCK = threading.Lock()

_USE_TERM_DESCRIPTION = (
    "Run one command from a pre-approved allowlist inside this deployment's "
    "working root. Pass `argv` as a LIST of strings, e.g. "
    '["ls", "-la"] - there is no shell, so ";", "|" and "&&" are ordinary '
    "text and will not chain commands. A command outside the allowlist is "
    "refused; check `error_kind` before reporting a result."
)

_READ_FILE_DESCRIPTION = (
    "Read a UTF-8 text file by path relative to this deployment's root. "
    "Paths that resolve outside that root - including via `..` or a symlink "
    "- are refused. Long files come back truncated with `truncated: true`; "
    "never state that a file ends where a truncated read ends."
)


def build_operator_tool_specs(
    policy: TerminalPolicy | None = None,
) -> tuple[ToolSpec, ...]:
    """Return the `use_term` and `read_file` specs over *policy*.

    Both are tiered `Tier.T3` (ADR-002 C.10), which alone already makes the
    Layer-2 interceptor revalidate them at call time (`tier in (T2, T3)`) —
    their permissions do not start with `write:`/`send:`, so the tier is the
    only thing that catches them. `always_revalidate=True` is ALSO set on
    both, intentionally redundant with the tier: a read that can reach any
    file under the root, or a command that can change the host, is exactly
    the "sensitive" case this opt-in exists for, and keeping it set is
    defense in depth against a future change to the tier-derivation logic
    ever silently un-classifying these two tools.
    """
    if policy is None:
        # Memoised so both shipped registries share ONE spec object per tool.
        # Every other platform connector is a module-level function, so a test
        # asserts the two registries wire the same callable and catches them
        # drifting apart; a closure rebuilt per call would defeat that.
        #
        # Locked because the bare check-then-set is a race: two concurrent
        # callers could each build a tuple and receive different objects,
        # which is exactly the identity invariant the memo exists for.
        global _UNCONFIGURED_SPECS
        with _UNCONFIGURED_LOCK:
            if _UNCONFIGURED_SPECS is None:
                _UNCONFIGURED_SPECS = _refusing_specs()
            return _UNCONFIGURED_SPECS

    return _build_specs(policy)


def _refusing_specs() -> tuple[ToolSpec, ...]:
    """The same two tools, wired to connectors that refuse everything.

    Registered rather than omitted because a tool a manifest names but the
    registry lacks makes the WHOLE role unbuildable through `InjectionError`
    -- so `operator-agent` would not boot at all. The choice is between a
    refusing tool and no operator role, and a refusing tool is the safe half.
    """
    return _specs(
        _unconfigured_connector("use_term"), _unconfigured_connector("read_file")
    )


def _build_specs(resolved: TerminalPolicy) -> tuple[ToolSpec, ...]:
    return _specs(
        build_terminal_connector(resolved), build_file_reader_connector(resolved)
    )


def _specs(term: AsyncConnector, reader: AsyncConnector) -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="use_term",
            description=_USE_TERM_DESCRIPTION,
            required_permissions=("exec:command",),
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Command and arguments as separate strings. Never "
                            "a single command line."
                        ),
                    }
                },
                "required": ["argv"],
            },
            connector=term,
            tier=Tier.T3,
            always_revalidate=True,
        ),
        ToolSpec(
            name="read_file",
            description=_READ_FILE_DESCRIPTION,
            required_permissions=("read:files",),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the deployment root.",
                    }
                },
                "required": ["path"],
            },
            connector=reader,
            tier=Tier.T3,
            always_revalidate=True,
        ),
    )
