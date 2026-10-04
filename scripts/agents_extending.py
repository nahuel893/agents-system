#!/usr/bin/env python3
"""List every agent that extends a given agent (#227, ADR-010 part 3).

A change to a reference agent runs that agent's live scenarios and those of
every agent that extends it (the Definition of done in `AGENTS.md`). This reads
the `extends:` graph from the `manifest.md` of every agent folder under
`platform/roles/` and `examples/agents/`:

    python scripts/agents_extending.py operator-agent     # -> developer-agent
    python scripts/agents_extending.py agent --direct     # direct children only

An agent is identified by its folder name. The three spellings of `extends:`
(`platform/roles/agent`, `../operator-agent`, `sales-agent`) all resolve to the
last path segment. Exit status 2 when the agent is not in either root.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = [REPO / "platform" / "roles", REPO / "examples" / "agents"]


class UnknownAgentError(LookupError):
    """The requested agent has no folder under any root."""


def _extends_of(manifest: pathlib.Path) -> str | None:
    text = manifest.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    front = yaml.safe_load(text.split("---", 2)[1]) or {}
    value = front.get("extends")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().rstrip("/").rsplit("/", 1)[-1]


def build_graph(roots: list[pathlib.Path]) -> dict[str, str | None]:
    """Map each agent folder name to the name it extends (None for a root)."""
    graph: dict[str, str | None] = {}
    for root in roots:
        for manifest in sorted(root.glob("*/manifest.md")):
            graph[manifest.parent.name] = _extends_of(manifest)
    return graph


def agents_extending(
    name: str, roots: list[pathlib.Path] | None = None, *, direct: bool = False
) -> list[str]:
    """Sorted names of the agents that extend *name*, transitively unless
    *direct*."""
    graph = build_graph(roots if roots is not None else DEFAULT_ROOTS)
    if name not in graph:
        raise UnknownAgentError(
            f"unknown agent {name!r}; known: {', '.join(sorted(graph))}"
        )
    found: set[str] = set()
    frontier = {name}
    while frontier:
        children = {
            child
            for child, parent in graph.items()
            if parent in frontier and child != name and child not in found
        }
        found |= children
        frontier = set() if direct else children
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("agent", help="folder name of the agent, e.g. operator-agent")
    parser.add_argument("--direct", action="store_true", help="direct children only")
    args = parser.parse_args(argv)
    try:
        names = agents_extending(args.agent, direct=args.direct)
    except UnknownAgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for item in names:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
