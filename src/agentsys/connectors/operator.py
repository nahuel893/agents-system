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
import signal
import threading
from typing import Any, Awaitable, Callable

import structlog

from agentsys.harness.registry import ToolSpec

_logger = structlog.get_logger()

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]


@dataclasses.dataclass(frozen=True)
class TerminalPolicy:
    """What an operator agent is allowed to do on the host.

    `root` and `allowed_commands` are REQUIRED and have no defaults. A default
    root would be whatever directory the process happened to start in, and a
    default allowlist would be a guess about which commands are safe — neither
    is a decision this library can make for a deployment it has never seen.
    """

    root: pathlib.Path
    """Working directory for every command, and the ceiling for every read."""

    allowed_commands: frozenset[str]
    """Exact `argv[0]` values that may run. Empty means nothing runs."""

    timeout_s: float = 10.0
    """Wall clock per command. A hung command is killed, not awaited."""

    max_output_bytes: int = 8192
    """Cap on returned stdout and file content.

    Unbounded output is a denial of service against the model's context
    window, and a large read is also the shape data exfiltration takes.
    """


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


async def _read_capped(stream: Any, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` bytes plus one, so truncation is detectable.

    `communicate()` buffers the whole payload before anything is truncated,
    so a command writing fast can exhaust memory while the tool dutifully
    reports `truncated: true`. Pipe throughput is gigabytes per second, so
    the wall-clock timeout is no bound here.
    """
    if stream is None:
        return b"", False
    data = await stream.read(limit + 1)
    return data[:limit], len(data) > limit


def build_terminal_connector(policy: TerminalPolicy) -> AsyncConnector:
    """Build `use_term` over *policy*."""

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

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(policy.root),
                env=_child_env(policy),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Its own process group, so the timeout below can kill the
                # whole tree rather than only the direct child.
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            return _refuse(
                "spawn_failed",
                f"could not start '{program}': {error}",
                program=program,
            )

        async def _drain() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
            out, err = await asyncio.gather(
                _read_capped(process.stdout, policy.max_output_bytes),
                _read_capped(process.stderr, policy.max_output_bytes),
            )
            await process.wait()
            return out, err

        try:
            (stdout, out_cut), (stderr, err_cut) = await asyncio.wait_for(
                _drain(), timeout=policy.timeout_s
            )
        except TimeoutError:
            _kill_group(process)
            return _refuse(
                "timeout",
                f"'{program}' exceeded {policy.timeout_s}s and was killed.",
                program=program,
            )

        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "truncated": out_cut or err_cut,
        }

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

        if not candidate.is_file():
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

    Both are marked `always_revalidate=True`. Their permissions do not start
    with `write:` or `send:`, so the Layer-2 interceptor would not otherwise
    re-check them at call time - and a read that can reach any file under the
    root, or a command that can change the host, is exactly the "sensitive
    read" that opt-in exists for.
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
            always_revalidate=True,
        ),
    )
