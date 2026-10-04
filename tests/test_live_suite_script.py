"""Offline tests for `scripts/live_suite_cost.py` and the structure of
`scripts/run-live-suite.sh` (#227, ADR-010 part 3)."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

from scripts.live_suite_cost import total_cost

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "scripts" / "run-live-suite.sh").read_text(encoding="utf-8")


def _report(path: pathlib.Path, *costs: float | None) -> None:
    path.write_text(
        json.dumps(
            [{"scenario": f"s{i}", "total_cost_usd": c} for i, c in enumerate(costs)]
        ),
        encoding="utf-8",
    )


def test_total_cost_sums_reports_newer_than_the_marker(tmp_path: pathlib.Path) -> None:
    old = tmp_path / "old.json"
    _report(old, 9.0)
    marker = tmp_path / "marker"
    marker.touch()
    os.utime(old, (1, 1))
    os.utime(marker, (100, 100))
    new = tmp_path / "new.json"
    _report(new, 0.25, 0.5)
    os.utime(new, (200, 200))
    assert total_cost(tmp_path, marker) == 0.75


def test_total_cost_is_none_when_no_report_carries_a_cost(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    os.utime(marker, (100, 100))
    report = tmp_path / "r.json"
    _report(report, None)
    os.utime(report, (200, 200))
    assert total_cost(tmp_path, marker) is None


def test_total_cost_falls_back_to_the_suite_log_turn_usage_lines(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    log = tmp_path / "suite.log"
    usage = {"event": "runtime.turn_usage", "cost_usd": 0.01}
    other = {"event": "something.else", "cost_usd": 5.0}
    log.write_text(
        "\n".join(
            ["plain text", json.dumps(usage), json.dumps(usage), json.dumps(other)]
            + ["{not json"]
        ),
        encoding="utf-8",
    )
    assert total_cost(tmp_path, marker, log) == 0.02


def test_reports_win_over_the_log_so_nothing_is_counted_twice(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    os.utime(marker, (100, 100))
    report = tmp_path / "r.json"
    _report(report, 0.5)
    os.utime(report, (200, 200))
    log = tmp_path / "suite.log"
    log.write_text(
        json.dumps({"event": "runtime.turn_usage", "cost_usd": 9.0}), encoding="utf-8"
    )
    assert total_cost(tmp_path, marker, log) == 0.5


def test_total_cost_is_none_when_any_report_row_has_unknown_cost(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    os.utime(marker, (100, 100))
    report = tmp_path / "r.json"
    _report(report, 0.3, None)
    os.utime(report, (200, 200))
    assert total_cost(tmp_path, marker) is None


def test_total_cost_is_none_when_any_turn_usage_line_has_unknown_cost(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    log = tmp_path / "suite.log"
    known = {"event": "runtime.turn_usage", "cost_usd": 0.01}
    unknown = {"event": "runtime.turn_usage", "cost_usd": None}
    log.write_text("\n".join(json.dumps(r) for r in (known, unknown)), encoding="utf-8")
    assert total_cost(tmp_path, marker, log) is None


def test_group_cost_is_n_a_when_any_suite_cost_is_unknown() -> None:
    assert 'if ($1 == "n/a") u = 1' in SCRIPT
    assert "if (u || !n) print" in SCRIPT


def test_cli_prints_n_a_for_no_cost(tmp_path: pathlib.Path) -> None:
    marker = tmp_path / "marker"
    marker.touch()
    out = subprocess.run(
        [sys.executable, "scripts/live_suite_cost.py", str(tmp_path), str(marker)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "n/a"


def test_suite_script_is_valid_bash() -> None:
    subprocess.run(
        ["bash", "-n", str(REPO / "scripts" / "run-live-suite.sh")], check=True
    )


def test_suite_defaults_model_prices_for_the_reference_model() -> None:
    assert re.search(r"default_prices='.*openai/gpt-6-luna", SCRIPT)
    assert 'MODEL_PRICES="${MODEL_PRICES:-$default_prices}"' in SCRIPT
    assert "export MODEL_PRICES" in SCRIPT


def test_suite_groups_are_mechanism_behavior_composition() -> None:
    groups = re.findall(r"^run_group\s+(\w+)", SCRIPT, flags=re.MULTILINE)
    assert groups == ["mechanism", "behavior", "composition"]


def test_every_pytest_module_the_suite_runs_exists() -> None:
    for module in re.findall(r"tests/(test_\w+\.py)", SCRIPT):
        assert (REPO / "tests" / module).is_file(), module
