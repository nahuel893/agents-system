"""``agents check <agent>``: what an agent resolves to, before it runs.

It resolves the agent and builds its runtime through the same
``resolve``/``build_runtime`` a library caller uses, then prints the chain it
extends, what it declares, and each declared tool as equipped or denied with
the reason (issue #312). The reasons come from ``injector.explain_denial``,
the same decision the runtime takes.
"""

from __future__ import annotations

import argparse
import dataclasses
import warnings
from collections.abc import Mapping
from typing import Any, TextIO

from agents_system.cli._common import (
    CLIError,
    check_grants,
    load_agent,
    load_registry,
)

#: Shown for a declared tool the registry does not hold, and no built-in
#: default fills. The served app fails boot on it; a library build raises.
NOT_IN_REGISTRY = "not in the registry"


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "check",
        help="show what an agent resolves to: its tools, permissions and limits",
        description=(
            "Resolve an agent and print the chain it extends, its declared "
            "permissions, each declared tool as equipped or denied (and why), "
            "its execution limits and its memory policy."
        ),
    )
    parser.add_argument(
        "agent", help="an agent folder (./my-agent) or a predefined role name"
    )
    add_runtime_flags(parser)
    parser.set_defaults(run=run)


def add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    """``--grant`` and ``--registry``, shared with ``agents chat``."""
    parser.add_argument(
        "--grant",
        action="append",
        default=[],
        metavar="PERM",
        help="a permission the caller grants, e.g. read:catalog (repeatable)",
    )
    parser.add_argument(
        "--registry",
        metavar="MODULE:ATTR",
        help=(
            "your ToolRegistry, or a callable returning one "
            "(default: the platform's reference tools)"
        ),
    )


@dataclasses.dataclass(frozen=True)
class ToolStatus:
    name: str
    equipped: bool
    #: For an equipped tool, a note ("built-in default"); for a denied one,
    #: each reason it is denied.
    notes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Report:
    definition: Any
    chain: tuple[str, ...]
    registry: str
    grants: tuple[str, ...]
    tools: tuple[ToolStatus, ...]
    limits: Mapping[str, Any]
    #: What resolving the agent warned about, such as a deprecated
    #: predefined role named by the caller.
    warnings: tuple[str, ...] = ()


def inspect_agent(
    target: str, grants: list[str], registry_target: str | None
) -> Report:
    """Resolve ``target`` and describe each tool it declares."""
    from agents_system.agent.graph import _effective_limits
    from agents_system.connectors.command_tools import build_command_tool_specs
    from agents_system.harness.defaults import builtin_default_tools
    from agents_system.harness.factory import FactoryError, build_runtime
    from agents_system.harness.injector import InjectionError, explain_denial
    from agents_system.harness.loader import DefinitionError, extends_chain, resolve
    from agents_system.permissions import AgentPermissionError

    agent = load_agent(target)
    registry = load_registry(registry_target)
    grants = check_grants(grants)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            definition = resolve(agent)
        defaults = builtin_default_tools(definition)
        missing = [
            name
            for name in definition.tools
            if name not in registry and name not in defaults
        ]
        # A tool nothing provides is reported, not raised on: the rest of
        # the surface is still worth seeing.
        equipped = build_runtime(
            agent,
            registry,
            grants,
            definition=dataclasses.replace(
                definition,
                tools=tuple(name for name in definition.tools if name not in missing),
            ),
        )
        chain = extends_chain(agent)
        limits = _effective_limits(definition.execution_limits)
    except (
        AgentPermissionError,
        DefinitionError,
        FactoryError,
        InjectionError,
        ValueError,
    ) as error:
        raise CLIError(f"{target}: {error}") from error

    granted = {spec.name for spec in equipped.tools}
    specs = {
        name: registry.get(name) if name in registry else defaults[name]
        for name in definition.tools
        if name not in missing
    }
    specs.update(
        {spec.name: spec for spec in build_command_tool_specs(definition.command_tools)}
    )
    tools: list[ToolStatus] = []
    for name, spec in specs.items():
        if name in granted:
            notes = ("built-in default",) if name in equipped.builtin_defaults else ()
            tools.append(ToolStatus(name, True, notes))
        else:
            tools.append(
                ToolStatus(name, False, explain_denial(spec, definition, grants))
            )
    tools.extend(ToolStatus(name, False, (NOT_IN_REGISTRY,)) for name in missing)
    # As declared: the registry tools, then the command tools.
    order: dict[str, int] = {}
    for name in [*definition.tools, *specs]:
        order.setdefault(name, len(order))
    tools.sort(key=lambda status: order[status.name])
    return Report(
        definition,
        chain,
        registry_target or "the platform's reference tools (no --registry)",
        tuple(grants),
        tuple(tools),
        limits,
        tuple(dict.fromkeys(str(warning.message) for warning in caught)),
    )


def render(report: Report, out: TextIO) -> None:
    definition = report.definition
    write = out.write
    write(f"Agent: {definition.role_name} (version {definition.version})\n")
    write(f"  extends:          {' -> '.join(report.chain)}\n")
    write(f"  permissions:      {_listed(definition.permissions)}\n")
    write(f"  registry:         {report.registry}\n")
    write(f"  granted:          {_listed(report.grants)}\n")
    write(f"  autonomy:         {definition.autonomy or '(none)'}\n")
    write(f"  untrusted input:  {'yes' if definition.untrusted_input else 'no'}\n")
    model = definition.model
    write(
        "  model:            "
        + (f"{model['provider']} / {model['model']}" if model else "(not declared)")
        + "\n"
    )

    write("\nTools:\n")
    if not report.tools:
        write("  (none declared)\n")
    width = max((len(status.name) for status in report.tools), default=0)
    for status in report.tools:
        if status.equipped:
            note = f" ({', '.join(status.notes)})" if status.notes else ""
            write(f"  {status.name:<{width}}  equipped{note}\n")
            continue
        write(f"  {status.name:<{width}}  denied: {status.notes[0]}\n")
        for reason in status.notes[1:]:
            write(f"  {'':<{width}}          {reason}\n")

    write("\nExecution limits:\n")
    for key, value in report.limits.items():
        write(f"  {key}: {value}\n")

    write("\nMemory policy:\n")
    if not definition.memory_policy:
        write("  (none)\n")
    for key, value in definition.memory_policy.items():
        write(f"  {key}: {value}\n")

    if report.warnings:
        write("\nWarnings:\n")
        for message in report.warnings:
            write(f"  {message}\n")


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "(none)"


def run(args: argparse.Namespace, out: TextIO) -> int:
    render(inspect_agent(args.agent, args.grant, args.registry), out)
    return 0
