#!/usr/bin/env python3
"""Total the cost of one live suite (#227).

`scripts/run-live-suite.sh` calls this after each suite so its summary shows
what every suite and group cost. Usage:

    python scripts/live_suite_cost.py evals/results <marker-file> [<suite-log>]

Suites that run through the eval runner write reports to `evals/results/`; the
cost is the total of the reports newer than the marker file. Suites that boot
the harness write no report, but the runtime logs one `runtime.turn_usage`
line per turn; when no report carries a cost, the cost of those lines in the
suite log is used instead, so nothing is counted twice. Prints the total in
USD, or `n/a` when neither source has a cost (the model had no `MODEL_PRICES`
entry).
"""

from __future__ import annotations

import json
import pathlib
import sys


def _report_cost(results_dir: pathlib.Path, marker: pathlib.Path) -> float | None:
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


def _log_cost(log: pathlib.Path) -> float | None:
    total: float | None = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "runtime.turn_usage" and row.get("cost_usd") is not None:
            total = (total or 0.0) + float(row["cost_usd"])
    return total


def total_cost(
    results_dir: pathlib.Path, marker: pathlib.Path, log: pathlib.Path | None = None
) -> float | None:
    total = _report_cost(results_dir, marker) if results_dir.is_dir() else None
    if total is None and log is not None and log.is_file():
        total = _log_cost(log)
    return total


def main(argv: list[str]) -> int:
    log = pathlib.Path(argv[3]) if len(argv) > 3 else None
    total = total_cost(pathlib.Path(argv[1]), pathlib.Path(argv[2]), log)
    print("n/a" if total is None else f"{total:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
