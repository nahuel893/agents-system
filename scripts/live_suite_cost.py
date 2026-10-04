#!/usr/bin/env python3
"""Total the cost of the eval reports written since a marker file (#227).

`scripts/run-live-suite.sh` calls this after each group so its summary shows
what the group cost. Usage:

    python scripts/live_suite_cost.py evals/results <marker-file>

Prints the total in USD, or `n/a` when no report carries a cost (the model had
no `MODEL_PRICES` entry).
"""

from __future__ import annotations

import json
import pathlib
import sys


def total_cost(results_dir: pathlib.Path, marker: pathlib.Path) -> float | None:
    since = marker.stat().st_mtime
    total: float | None = None
    for report in sorted(results_dir.glob("*.json")):
        if report.stat().st_mtime <= since:
            continue
        for row in json.loads(report.read_text(encoding="utf-8")):
            cost = row.get("total_cost_usd")
            if cost is not None:
                total = (total or 0.0) + float(cost)
    return total


def main(argv: list[str]) -> int:
    results_dir, marker = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    total = total_cost(results_dir, marker) if results_dir.is_dir() else None
    print("n/a" if total is None else f"{total:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
