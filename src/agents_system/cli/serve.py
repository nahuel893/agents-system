"""``agents serve``: run the served app (``agents_system.server``) on uvicorn.

The flags map onto what the app already reads: ``--agents-root`` is
``AGENTS_ROOT`` and ``--registry`` is ``REGISTRY_FACTORY``. Everything else the
app needs (its database, its registrations, its grants) still comes from the
environment and ``.env``, as with ``uvicorn agents_system.server:app``.
"""

from __future__ import annotations

import argparse
import os
import pathlib
from typing import Any, TextIO

from agents_system.cli._common import CLIError
from agents_system.extras import raise_missing_extra

#: The app factory uvicorn imports, in each worker.
APP_FACTORY = "agents_system.server:get_application"


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "serve",
        help="run the served app (needs the server extra)",
        description=(
            "Run the served app on uvicorn. Its configuration comes from the "
            "environment and .env; the flags below set the parts of it they name."
        ),
    )
    parser.add_argument(
        "--agents-root",
        metavar="DIR",
        help="the folder agent registrations are read from (sets AGENTS_ROOT)",
    )
    parser.add_argument(
        "--registry",
        metavar="MODULE:ATTR",
        help="the registry factory the app builds its tools with (sets REGISTRY_FACTORY)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="the address to bind (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="the port to bind (default: 8000)"
    )
    parser.add_argument(
        "--workers", type=int, help="worker processes (default: uvicorn's, one)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart when the code changes (development)",
    )
    parser.set_defaults(run=run)


def run(args: argparse.Namespace, out: TextIO) -> int:
    try:
        import uvicorn

        import agents_system.server  # noqa: F401 -- fail here, naming the extra
    except ImportError as error:
        raise_missing_extra("server", "agents serve", error)

    if args.agents_root is not None:
        root = pathlib.Path(args.agents_root).resolve()
        if not root.is_dir():
            raise CLIError(f"--agents-root {args.agents_root}: not a directory")
        # The served app refuses a relative AGENTS_ROOT.
        os.environ["AGENTS_ROOT"] = str(root)
    if args.registry is not None:
        os.environ["REGISTRY_FACTORY"] = args.registry

    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
    )
    return 0
