"""What every subcommand shares: its error, its logging, and how it reads an
agent, a registry and the caller's grants from the command line."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import pathlib
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from agents_system.agent.spec import Agent
    from agents_system.harness.registry import ToolRegistry


class CLIError(Exception):
    """A failure the command reports in one line, and exits 1 on."""


def setup_logging(verbose: bool) -> None:
    """Send the runtime's structured logs to stderr: errors only, or
    everything with ``--verbose``. stdout carries the command's own output.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.ERROR
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )


def load_agent(target: str) -> Agent | str:
    """The agent ``target`` names: a folder (``./my-agent``), loaded with
    ``Agent.from_folder``, or the name of a predefined role (``agent``).

    A value that looks like a path must be a folder; anything else is passed
    to the loader as a role name, which says so if no such role exists.
    """
    from agents_system.agent.spec import Agent

    path = pathlib.Path(target)
    if path.is_dir():
        return Agent.from_folder(path)
    if os.sep in target or "/" in target or target.startswith("."):
        raise CLIError(f"{target}: no such agent folder")
    return target


def _import_target(target: str, flag: str) -> Any:
    module_name, sep, attribute = target.partition(":")
    if not sep or not module_name or not attribute:
        raise CLIError(
            f"{flag} {target!r}: expected module:attribute, e.g. my_app.tools:registry"
        )
    # The project the command runs in, as `python -m` and uvicorn see it.
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise CLIError(f"{flag} {target!r}: cannot import {module_name}: {error}")
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise CLIError(f"{flag} {target!r}: {module_name} has no {attribute}") from None


def load_registry(target: str | None) -> ToolRegistry:
    """The registry ``--registry module:attribute`` names, or the platform's
    reference tools when there is none.

    The attribute is a ``ToolRegistry``, or a callable that returns one: a
    factory taking no arguments, or the served app's registry factory, which
    is called with ``settings``, ``embedder`` and ``bi_engine`` all ``None``.
    """
    from agents_system.harness.registry import ToolRegistry

    if target is None:
        return reference_registry()
    value = _import_target(target, "--registry")
    if callable(value) and not isinstance(value, ToolRegistry):
        try:
            inspect.signature(value).bind()
        except TypeError:
            value = value(None, None, None)
        else:
            value = value()
    if not isinstance(value, ToolRegistry):
        raise CLIError(
            f"--registry {target!r}: expected a ToolRegistry or a callable "
            f"returning one, got {type(value).__name__}"
        )
    return value


def reference_registry() -> ToolRegistry:
    """The platform's reference tools, and the report tool when the
    ``postgres`` extra it needs is installed."""
    from agents_system.connectors.reference_registry import build_reference_registry
    from agents_system.extras import MissingExtraError

    registry = build_reference_registry()
    try:
        from agents_system.connectors.report_connector import build_report_tool_spec
        from agents_system.connectors.sales_reports import CATALOG
    except MissingExtraError:
        return registry
    registry.register(build_report_tool_spec(None, CATALOG))
    return registry


def check_grants(grants: list[str]) -> list[str]:
    """``grants``, each a permission name the permission registry knows."""
    from agents_system.permissions import UnknownPermissionNameError, resolve

    for name in grants:
        try:
            resolve(name)
        except UnknownPermissionNameError as error:
            raise CLIError(f"--grant {name!r}: {error}") from None
    return grants
