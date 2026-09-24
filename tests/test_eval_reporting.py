"""Offline tests for live-eval result reporting (#169, ADR-002 E.18)."""

from __future__ import annotations

import datetime
import json
import pathlib

from agents_system.evals.reporting import DEFAULT_RESULTS_DIR, write_results
from agents_system.evals.runner import AssertionFailure, RunOutcome, ScenarioResult

_FIXED_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _result(
    *, scenario: str = "s1", role: str = "sales-agent", model: str = "fake-model"
) -> ScenarioResult:
    return ScenarioResult(
        scenario=scenario,
        role=role,
        model=model,
        runs=(
            RunOutcome(passed=True),
            RunOutcome(
                passed=False,
                failures=(AssertionFailure("tools_called", "expected catalog_search"),),
            ),
        ),
    )


def test_write_results_writes_a_json_file_with_every_scenarios_payload(
    tmp_path: pathlib.Path,
) -> None:
    out_dir = tmp_path / "results"

    json_path, _ = write_results([_result()], out_dir=out_dir, now=_FIXED_NOW)

    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload[0]["scenario"] == "s1"
    assert payload[0]["role"] == "sales-agent"
    assert payload[0]["model"] == "fake-model"
    assert payload[0]["success_rate"] == 0.5


def test_write_results_writes_a_markdown_summary_table(
    tmp_path: pathlib.Path,
) -> None:
    out_dir = tmp_path / "results"

    _, markdown_path = write_results([_result()], out_dir=out_dir, now=_FIXED_NOW)

    assert markdown_path.exists()
    text = markdown_path.read_text(encoding="utf-8")
    assert "s1" in text
    assert "sales-agent" in text
    assert "fake-model" in text
    assert "50%" in text


def test_write_results_creates_the_output_directory(tmp_path: pathlib.Path) -> None:
    out_dir = tmp_path / "nested" / "results"
    assert not out_dir.exists()

    write_results([_result()], out_dir=out_dir, now=_FIXED_NOW)

    assert out_dir.is_dir()


def test_write_results_default_dir_is_the_documented_gitignored_path() -> None:
    assert DEFAULT_RESULTS_DIR == pathlib.Path("evals/results")
