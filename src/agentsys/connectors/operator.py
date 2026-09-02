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
import pathlib
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return _refuse(
                "spawn_failed",
                f"could not start '{program}': {error}",
                program=program,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=policy.timeout_s
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return _refuse(
                "timeout",
                f"'{program}' exceeded {policy.timeout_s}s and was killed.",
                program=program,
            )

        out, out_cut = _truncate(stdout, policy.max_output_bytes)
        err, err_cut = _truncate(stderr, policy.max_output_bytes)

        return {
            "exit_code": process.returncode,
            "stdout": out,
            "stderr": err,
            "truncated": out_cut or err_cut,
        }

    return use_term


def build_file_reader_connector(policy: TerminalPolicy) -> AsyncConnector:
    """Build `read_file` over *policy*."""

    root = policy.root.resolve()

    async def read_file(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        raw_path = inputs.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return _refuse("invalid_path", "path must be a non-empty string.")

        # Resolve BEFORE deciding. `sub/../../etc/passwd` has no leading `..`,
        # and a symlink under the root can point anywhere on the host; a string
        # check sees neither.
        candidate = (root / raw_path).resolve()
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
            data = candidate.read_bytes()
        except OSError as error:
            return _refuse(
                "read_failed",
                f"could not read '{raw_path}': {error}",
                requested=raw_path,
            )

        content, truncated = _truncate(data, policy.max_output_bytes)
        return {"content": content, "truncated": truncated}

    return read_file


#: The policy a registry gets when the application configures none.
#:
#: Deliberately inert: no command is allowed, so `use_term` refuses every
#: call and `read_file` can only reach files under the process's own working
#: directory — and only if one is asked for by relative path. It exists so a
#: role declaring these tools can BOOT without the deployment having decided
#: its sandbox yet. A tool a manifest names but the registry lacks makes the
#: whole role unbuildable, so the choice is between an inert tool and no role
#: at all; an inert tool is the safe half of that pair.
#:
#: An application that actually wants terminal access builds its own
#: `TerminalPolicy` and registers these specs itself.
INERT_POLICY = TerminalPolicy(
    root=pathlib.Path.cwd(),
    allowed_commands=frozenset(),
)

_INERT_SPECS: tuple[ToolSpec, ...] | None = None

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
        # can assert the two registries wire the same callable and catch them
        # drifting apart. A closure rebuilt per call would defeat that, and
        # the inert policy has no per-caller state to keep them apart.
        global _INERT_SPECS
        if _INERT_SPECS is None:
            _INERT_SPECS = _build_specs(INERT_POLICY)
        return _INERT_SPECS

    return _build_specs(policy)


def _build_specs(resolved: TerminalPolicy) -> tuple[ToolSpec, ...]:
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
            connector=build_terminal_connector(resolved),
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
            connector=build_file_reader_connector(resolved),
            always_revalidate=True,
        ),
    )
