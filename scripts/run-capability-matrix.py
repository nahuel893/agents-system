#!/usr/bin/env python3
"""Run the capability suite on candidate models and print a model x capability
matrix (#228, ADR-010 part 4).

Usage:
    EVAL_PROVIDER=openai_compatible scripts/run-capability-matrix.py \\
        --model openai/gpt-6-luna --model google/gemini-3.8-flash \\
        --capability grounding --capability memory --runs 3

Models are built from the configured eval provider (``EVAL_PROVIDER`` and its
variables, see docs/platform/live-eval.md); pass ``MODEL_PRICES`` for every
model so cost is never ``n/a``. Writes JSON and Markdown under
``evals/results/`` (gitignored). Exits 1 when any cell misses its gate.

A cell is a rate for one model on the kit's generic prompt and a minimal
fixture agent, not a verdict on your own agent. This is a thin wrapper over
``agents_system.evals.matrix``.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections.abc import Sequence

from agents_system.evals.matrix import (
    candidates_from_names,
    run_capability_matrix,
    write_matrix,
)
from agents_system.evals.reporting import DEFAULT_RESULTS_DIR


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        help="candidate model name for the eval provider; repeat for several",
    )
    parser.add_argument(
        "--capability",
        action="append",
        help="capability id to run; repeat for several (default: all)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=None,
        help="runs per scenario (default: EVAL_RUNS, else 3)",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=DEFAULT_RESULTS_DIR,
        help="where the JSON and Markdown files go (default: evals/results)",
    )
    return parser.parse_args(argv)


async def amain(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    candidates = candidates_from_names(args.model)
    try:
        matrix = await run_capability_matrix(
            candidates, capabilities=args.capability, runs=args.runs
        )
    finally:
        for candidate in candidates:
            client = getattr(candidate.model, "http_async_client", None)
            if client is not None:
                await client.aclose()
    json_path, markdown_path = write_matrix(matrix, out_dir=args.out_dir)
    print(matrix.to_markdown())
    print(f"wrote {json_path} and {markdown_path}", file=sys.stderr)
    return 0 if all(c.gate_passed for c in matrix.cells if c.skipped is None) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
