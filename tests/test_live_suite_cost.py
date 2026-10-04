"""`scripts/live_suite_cost.py` and the group arithmetic of
`scripts/run-live-suite.sh` (#239), exercised offline."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "live_suite_cost", SCRIPTS / "live_suite_cost.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cost = _load()


def _marker(tmp_path: pathlib.Path) -> pathlib.Path:
    marker = tmp_path / "marker"
    marker.touch()
    os.utime(marker, (1, 1))
    return marker


def _results(tmp_path: pathlib.Path) -> pathlib.Path:
    results = tmp_path / "results"
    results.mkdir()
    return results


def _turn(cost_usd: float | None) -> str:
    return json.dumps({"event": "runtime.turn_usage", "cost_usd": cost_usd})


def test_sums_rows_of_a_report(tmp_path: pathlib.Path) -> None:
    results = _results(tmp_path)
    (results / "a.json").write_text(
        json.dumps([{"total_cost_usd": 0.5}, {"total_cost_usd": 0.25}])
    )
    assert cost.total_cost(results, _marker(tmp_path)) == pytest.approx(0.75)


@pytest.mark.parametrize(
    "payload",
    ['{"rows": []}', "not json", '[1, "x"]'],
)
def test_a_report_that_is_not_rows_of_dicts_is_skipped_with_a_warning(
    tmp_path: pathlib.Path, payload: str, capsys: pytest.CaptureFixture[str]
) -> None:
    results = _results(tmp_path)
    (results / "bad.json").write_text(payload)
    (results / "good.json").write_text(json.dumps([{"total_cost_usd": 1.0}]))

    assert cost.total_cost(results, _marker(tmp_path)) == pytest.approx(1.0)
    assert "bad.json" in capsys.readouterr().err


def test_unknown_cost_row_does_not_fall_back_to_the_log_total(
    tmp_path: pathlib.Path,
) -> None:
    results = _results(tmp_path)
    (results / "a.json").write_text(
        json.dumps([{"total_cost_usd": 1.0}, {"total_cost_usd": None}])
    )
    log = tmp_path / "suite.log"
    log.write_text(_turn(0.01) + "\n")

    assert cost.total_cost(results, _marker(tmp_path), log) is None


def test_log_total_is_used_when_no_report_exists(tmp_path: pathlib.Path) -> None:
    results = _results(tmp_path)
    log = tmp_path / "suite.log"
    log.write_text(_turn(0.01) + "\n" + _turn(0.02) + "\n")

    assert cost.total_cost(results, _marker(tmp_path), log) == pytest.approx(0.03)


def test_log_with_an_unknown_turn_cost_is_unknown(tmp_path: pathlib.Path) -> None:
    results = _results(tmp_path)
    log = tmp_path / "suite.log"
    log.write_text(_turn(0.01) + "\n" + _turn(None) + "\n")

    assert cost.total_cost(results, _marker(tmp_path), log) is None


def _fake_repo(tmp_path: pathlib.Path, stub: str) -> pathlib.Path:
    """A copy of the script with stubbed docker, pytest and python."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "tests" / "fixtures" / "sql_query_fixture.sql").write_text("")
    (repo / "scripts" / "provision_sql_readonly.sql").write_text("")
    shutil.copy(SCRIPTS / "run-live-suite.sh", repo / "scripts")
    (repo / ".venv" / "bin").mkdir(parents=True)
    pytest_stub = repo / ".venv" / "bin" / "pytest"
    pytest_stub.write_text(stub)
    pytest_stub.chmod(0o755)
    py = repo / ".venv" / "bin" / "python"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'cost="$(rg -o "COST=.*" "$4" 2>/dev/null | head -1 | cut -d= -f2)"\n'
        'echo "${cost:-n/a}"\n'
    )
    py.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "ps" ]]; then echo fake-container; exit 0; fi\n'
        "cat >/dev/null 2>&1 || true\n"
        "echo 1\n"
    )
    docker.chmod(0o755)
    bwrap = bindir / "bwrap"
    bwrap.write_text("#!/usr/bin/env bash\n")
    bwrap.chmod(0o755)
    return repo


def _run_script(repo: pathlib.Path, tmp_path: pathlib.Path) -> str:
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    subprocess.run(
        ["bash", str(repo / "scripts" / "run-live-suite.sh"), "--quick"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    summaries = list((repo / "evals" / "results").glob("live-suite-*/SUMMARY.txt"))
    assert len(summaries) == 1
    return summaries[0].read_text()


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_group_arithmetic_sums_costs_and_flags_failures(
    tmp_path: pathlib.Path,
) -> None:
    # Each suite prints COST=<value> (read by the stub python) and the file it
    # runs names the outcome: tests/test_live_eval_guardrails.py fails.
    stub = (
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *test_mechanism_suite*) echo COST=0.1 ;;\n"
        "  *test_live_load_phase3*) echo COST=0.2 ;;\n"
        "  *test_live_eval_capabilities*) echo COST=1.0 ;;\n"
        "  *test_live_eval_guardrails*) echo COST=2.0; exit 1 ;;\n"
        "  *test_live_eval_sql_query*) echo COST=n/a ;;\n"
        "  *test_live_smoke*) echo COST=0.5 ;;\n"
        "  *) echo COST=0.25 ;;\n"
        "esac\n"
    )
    repo = _fake_repo(tmp_path, stub)
    summary = _run_script(repo, tmp_path)

    lines = {line.split()[0]: line for line in summary.splitlines()[1:4]}
    assert "PASS" in lines["mechanism"] and "cost_usd=0.3000" in lines["mechanism"]
    # one unknown suite makes the whole group unknown, a failure marks it FAIL
    assert "FAIL" in summary.split("behavior")[1].splitlines()[0]
    assert "cost_usd=n/a" in summary.split("behavior")[1].splitlines()[0]
    comp = summary.split("composition")[1].splitlines()[0]
    assert "PASS" in comp


@pytest.mark.parametrize("bad", ["unavailable", "abc", [1], {"a": 1}])
def test_a_row_with_an_invalid_cost_makes_the_suite_cost_unknown(
    tmp_path: pathlib.Path, bad: object
) -> None:
    results = _results(tmp_path)
    (results / "a.json").write_text(json.dumps([{"total_cost_usd": bad}]))
    (results / "b.json").write_text(json.dumps([{"total_cost_usd": 1.0}]))

    assert cost.total_cost(results, _marker(tmp_path)) is None


def test_a_row_with_an_invalid_cost_does_not_fall_back_to_the_log(
    tmp_path: pathlib.Path,
) -> None:
    results = _results(tmp_path)
    (results / "a.json").write_text(json.dumps([{"total_cost_usd": "unavailable"}]))
    log = tmp_path / "run.log"
    log.write_text(json.dumps({"event": "runtime.turn_usage", "cost_usd": 0.03}) + "\n")

    assert cost.total_cost(results, _marker(tmp_path), log) is None
