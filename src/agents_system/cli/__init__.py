"""The package's command line (issue #312).

``agents`` and its alias ``agents-system`` both run ``main``. Each subcommand
lives in its own module and imports what it needs only when it runs, so the
core install runs ``agents --help``, ``check`` and ``chat``, and a subcommand
whose extra is missing fails with the error naming it::

    agents check ./my-agent --grant read:catalog
    agents chat ./my-agent --provider anthropic --model claude-haiku-5-5
    agents serve --agents-root ./agents --port 8000
    agents db upgrade

Built on stdlib ``argparse``: the command adds no dependency to the core.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from agents_system.cli import chat, check, db, serve
from agents_system.cli._common import CLIError, setup_logging
from agents_system.extras import MissingExtraError

__all__ = ["CLIError", "main"]

_PROGRAM = "agents"


def build_parser() -> argparse.ArgumentParser:
    from agents_system import __version__

    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description="Build, inspect, run and serve agents-system agents.",
    )
    parser.add_argument(
        "--version", action="version", version=f"agents-system {__version__}"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the runtime's logs on stderr (default: errors only)",
    )
    subparsers = parser.add_subparsers(title="commands", metavar="COMMAND")
    for module in (check, chat, serve, db):
        module.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return the exit status.

    0 on success, 1 when the command fails (the reason is printed on stderr,
    without a traceback), 2 on a usage error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "run"):
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{_PROGRAM}: error: a command is required\n")
        return 2
    if args.run is not serve.run:
        # The served app configures its own logging.
        setup_logging(args.verbose)
    try:
        return int(args.run(args, sys.stdout))
    except (CLIError, MissingExtraError) as error:
        sys.stderr.write(f"{_PROGRAM}: error: {error}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130
