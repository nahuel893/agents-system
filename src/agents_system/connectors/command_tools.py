"""Declarative command tools from a manifest's `command_tools:` list
(ADR-002 C.12).

`harness.loader` already validated every `CommandToolDeclaration` at LOAD
time: `argv[0]` is an absolute path, every `{param}` placeholder occupies a
WHOLE argv element, every placeholder has a matching typed param, and every
param is used. What is LEFT for call time is exactly what a model-controlled
value can still do wrong: pass an unexpected param, pass a value of the
wrong shape, or pass a value that starts with `-` and tries to smuggle a
second flag into the command it is filling in for (the option-injection
class ADR-002 C.12's table names: `git -c core.sshCommand=...`,
`find -exec rm`, `psql -c "DROP TABLE ..."`, `curl -d @/etc/secret`).

The connector this module builds does exactly that check, substitutes the
validated values into the template, and executes the result through
`operator._run_argv` — the SAME no-shell subprocess seam `use_term` uses,
not a second command runner. Reusing that one seam is also what lets
ADR-002 C.14's bubblewrap sandbox wrap command tools and `use_term`
identically, with a single change in one place.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import tempfile
import unicodedata
from typing import Any, Awaitable, Callable

import structlog

from agents_system.connectors.operator import (
    ConnectorOutput,
    SandboxPolicy,
    TerminalPolicy,
    _refuse,
    _reject_null_bytes,
    _run_argv,
    log_sandbox_availability_at_boot,
)
from agents_system.harness.loader import CommandToolDeclaration, CommandToolParam
from agents_system.harness.registry import ToolSpec

_logger = structlog.get_logger()

AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]

#: A command tool has no deployment-configured limits (unlike `use_term`,
#: which is deliberately unconfigured-by-default and refuses everything
#: until a deployment supplies a `TerminalPolicy`) — these are fixed,
#: conservative platform defaults, matching `TerminalPolicy`'s own
#: dataclass defaults. `root` is NOT here: every call gets its own fresh
#: scratch workspace (`tempfile.mkdtemp`), created and torn down inside
#: `run_command_tool` itself, never a fixed or configurable path (ADR-002
#: C.14 review follow-up, PR #148 -- see `run_command_tool`'s own comment
#: for why a shared `Path.cwd()` root was a real vulnerability, not a
#: theoretical one).
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_MAX_OUTPUT_BYTES = 8192

_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")

#: U+2212 MINUS SIGN is Unicode category Sm (math symbol), not Pd (dash
#: punctuation) — every OTHER dash this guard cares about (en dash, em
#: dash, the plain ASCII hyphen-minus itself, ...) already is Pd, so this is
#: the one codepoint that needs an explicit check alongside the category one.
_MINUS_SIGN = "−"


def _starts_with_dash_like(char: str) -> bool:
    """True for `-` and every Unicode dash lookalike (ADR-002 C.12, PR #147
    review follow-up): category Pd (hyphen-minus, en dash, em dash, and
    every other Unicode dash) plus U+2212 MINUS SIGN, which is category Sm
    and would otherwise slip past a category-only check."""
    return unicodedata.category(char) == "Pd" or char == _MINUS_SIGN


def _validate_param_value(
    name: str, value: Any, spec: CommandToolParam
) -> tuple[str | None, ConnectorOutput | None]:
    """Validate one call-time param value against its declaration.

    Returns `(substituted_text, None)` on success or `(None, refusal)` on
    failure — the refusal is already a well-formed `ConnectorOutput`, ready
    to return straight from the connector.
    """
    if spec.type == "string":
        if not isinstance(value, str):
            return None, _refuse(
                "invalid_param", f"parameter '{name}' must be a string."
            )
        text = value
    elif spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, _refuse(
                "invalid_param", f"parameter '{name}' must be an integer."
            )
        text = str(value)
    else:  # pragma: no cover — the loader rejects any other type at load time
        return None, _refuse(
            "invalid_param", f"parameter '{name}' has an unsupported type."
        )

    # ADR-002 C.12's option-injection guard: `-c core.sshCommand=...`,
    # `-exec rm`, `-c "DROP TABLE ..."`, `-d @/etc/secret` all start with
    # `-`. Rejecting any value shaped like that closes the whole class,
    # independent of `pattern`/`enum` — a param author who forgets to write
    # a strict enough pattern is still covered by this. This also rejects
    # any value starting with a Unicode dash LOOKALIKE (en dash `–`, em dash
    # `—`, or any other Unicode category-Pd dash, plus U+2212 MINUS SIGN,
    # which is category Sm and would otherwise slip past a bare ASCII `-`
    # check) — a PR #147 review follow-up: those read as "this is a flag" to
    # a human and to many CLIs just as readily as a plain hyphen does. One
    # deliberate, documented consequence: a negative integer value (e.g.
    # `-1`) can never be passed through a command tool param — there is no
    # narrow way to tell "a negative number" apart from "an option flag" at
    # this layer, so both are refused (see `docs/platform/tool.md`).
    if text and _starts_with_dash_like(text[0]):
        return None, _refuse(
            "option_injection_rejected",
            f"parameter '{name}' may not start with '-' or a Unicode dash "
            "lookalike (en dash, em dash, minus sign, ...) — ADR-002 C.12 "
            "option-injection guard. This also means a negative number "
            "can never be passed through a command tool param.",
        )
    if spec.max_length is not None and len(text) > spec.max_length:
        return None, _refuse(
            "invalid_param",
            f"parameter '{name}' exceeds max_length={spec.max_length}.",
        )
    if spec.pattern is not None and re.fullmatch(spec.pattern, text) is None:
        return None, _refuse(
            "invalid_param", f"parameter '{name}' does not match the required pattern."
        )
    if spec.enum is not None and text not in spec.enum:
        return None, _refuse(
            "invalid_param",
            f"parameter '{name}' must be one of {list(spec.enum)}.",
        )
    return text, None


def build_command_tool_connector(declaration: CommandToolDeclaration) -> AsyncConnector:
    """Build the async connector for one declared command tool."""
    log_sandbox_availability_at_boot(context=f"command_tools:{declaration.name}")

    async def run_command_tool(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if not isinstance(inputs, dict):
            return _refuse("invalid_input", "inputs must be an object.")

        unknown = set(inputs) - set(declaration.params)
        if unknown:
            return _refuse("unknown_param", f"unknown parameter(s): {sorted(unknown)}.")
        missing = set(declaration.params) - set(inputs)
        if missing:
            return _refuse(
                "missing_param",
                f"missing required parameter(s): {sorted(missing)}.",
            )

        substituted: dict[str, str] = {}
        for pname, pspec in declaration.params.items():
            text, refusal = _validate_param_value(pname, inputs[pname], pspec)
            if refusal is not None:
                return refusal
            assert text is not None
            substituted[pname] = text

        argv: list[str] = []
        for element in declaration.argv:
            match = _PLACEHOLDER.match(element)
            argv.append(substituted[match.group(1)] if match else element)

        rejected = _reject_null_bytes(argv)
        if rejected is not None:
            return rejected

        # A FRESH scratch workspace per call, never the process's own `cwd`
        # (ADR-002 C.14 review follow-up, PR #148): `root=Path.cwd()` here
        # used to mean the platform's own working directory -- typically
        # the deployment's repository checkout -- was bound READ-WRITE
        # inside the sandbox, so a declared command tool could read AND
        # overwrite the real `.env` and `.git/config`. `TerminalPolicy.
        # __post_init__` now also refuses a `root` that resolves to `cwd`
        # (or `$HOME`, `/root`, `/`, ...) as a second, independent barrier,
        # but the real fix is that a command tool never had any business
        # touching the platform's own files: it needs somewhere to run,
        # not the repository. Removed unconditionally in `finally`, so a
        # timeout (`_run_argv` returns a normal refusal, not an exception)
        # and any unexpected exception both still clean it up.
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="agents_system-command-tool-"))
        try:
            policy = TerminalPolicy(
                root=workdir,
                allowed_commands=frozenset({declaration.argv[0]}),
                # A command tool's `argv[0]` is already resolved to an
                # absolute path at load time (ADR-002 C.12), so the fixed
                # system paths plus `_bwrap_argv`'s automatic bind of that
                # resolved path's own install directory (ADR-002 C.14) are
                # enough here -- no extra binds and no network by default,
                # same as `use_term`.
                sandbox=SandboxPolicy(),
                timeout_s=_DEFAULT_TIMEOUT_S,
                max_output_bytes=_DEFAULT_MAX_OUTPUT_BYTES,
            )
            return await _run_argv(argv, policy=policy)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return run_command_tool


def _input_schema(declaration: CommandToolDeclaration) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for pname, pspec in declaration.params.items():
        prop: dict[str, Any] = {
            "type": "string" if pspec.type == "string" else "integer"
        }
        if pspec.pattern is not None:
            prop["pattern"] = pspec.pattern
        if pspec.max_length is not None:
            prop["maxLength"] = pspec.max_length
        if pspec.enum is not None:
            prop["enum"] = list(pspec.enum)
        properties[pname] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(declaration.params),
    }


def build_command_tool_spec(declaration: CommandToolDeclaration) -> ToolSpec:
    """Turn one declared command tool into a live `ToolSpec`."""
    return ToolSpec(
        name=declaration.name,
        description=(
            f"Run the '{declaration.name}' command tool — a fixed, "
            "pre-approved command with typed parameters. There is no "
            "shell and no free-form argv: only the declared parameters "
            "vary."
        ),
        required_permissions=(declaration.permission,),
        input_schema=_input_schema(declaration),
        connector=build_command_tool_connector(declaration),
        tier=declaration.tier,
    )


def build_command_tool_specs(
    declarations: tuple[CommandToolDeclaration, ...],
) -> tuple[ToolSpec, ...]:
    """Turn every declared command tool on a resolved role into `ToolSpec`s."""
    return tuple(build_command_tool_spec(decl) for decl in declarations)
