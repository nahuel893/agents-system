"""Write live-eval results to a gitignored directory (#169, ADR-002 E.18).

See `docs/platform/live-eval.md` for the documented output path and format.
"""

from __future__ import annotations

import datetime
import json
import pathlib
from collections.abc import Iterable

from agents_system.evals.runner import ScenarioResult

#: Documented default output directory, relative to the current working
#: directory (a repository checkout root in every shipped usage). Generated,
#: run-specific, and gitignored -- see `.gitignore`'s `evals/results/` entry.
#: `evals/scenarios/` (the tracked YAML scenario files) is a sibling, not a
#: parent, of this path.
DEFAULT_RESULTS_DIR = pathlib.Path("evals/results")


def write_results(
    results: Iterable[ScenarioResult],
    *,
    out_dir: pathlib.Path = DEFAULT_RESULTS_DIR,
    now: datetime.datetime | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write *results* as one timestamped JSON file plus a short markdown
    summary table. Returns `(json_path, markdown_path)`.
    """
    results = list(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y%m%dT%H%M%SZ")

    json_path = out_dir / f"{timestamp}.json"
    json_path.write_text(
        json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    markdown_path = out_dir / f"{timestamp}.md"
    lines = [
        f"# Live-eval results -- {timestamp}",
        "",
        "| Scenario | Role | Model | Runs | Success rate | Tokens | Cost (USD) | Duration (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        total_usage = result.total_usage
        # #78 Phase 0 -- "n/a" (never a guessed 0) whenever a run's model
        # reported no usage_metadata -- see TurnUsage's honesty rule.
        tokens = (
            "n/a"
            if total_usage is None or total_usage.total_tokens is None
            else str(total_usage.total_tokens)
        )
        cost = (
            "n/a"
            if total_usage is None or total_usage.cost_usd is None
            else f"{total_usage.cost_usd:.4f}"
        )
        # #78 Phase 0 Slice 2 -- "n/a" only when a run crashed before its
        # first turn returned (ScenarioResult.total_duration_s's own
        # honesty rule); a real duration is never a guessed 0 either.
        duration = (
            "n/a"
            if result.total_duration_s is None
            else f"{result.total_duration_s:.2f}"
        )
        lines.append(
            f"| {result.scenario} | {result.role} | {result.model} | "
            f"{len(result.runs)} | {result.success_rate:.0%} | {tokens} | {cost} | "
            f"{duration} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, markdown_path
