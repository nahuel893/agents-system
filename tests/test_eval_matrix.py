"""Offline tests for the per-model capability matrix (#228, ADR-010 part 4).

Nothing here calls a model: the matrix is built from scripted
`ScenarioResult`s injected through `run_case`, and one test drives the real
`run_capability_scenario` with a scripted chat model to prove the wiring.
"""

from __future__ import annotations

import json
import pathlib
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents_system.agent.graph import TurnUsage
from agents_system.evals import matrix as matrix_module
from agents_system.evals.capabilities import CAPABILITIES, load_capability_scenarios
from agents_system.evals.matrix import (
    CapabilityMatrix,
    MatrixCase,
    ModelCandidate,
    run_capability_matrix,
    scenario_case,
    write_matrix,
)
from agents_system.evals.runner import RunOutcome, ScenarioResult
from agents_system.evals.schema import (
    CATEGORY_GUARDRAIL,
    CATEGORY_HAPPY_PATH,
    Scenario,
)


def _usage(tokens: int | None, cost: float | None) -> TurnUsage:
    return TurnUsage(
        model_calls=1,
        input_tokens=tokens,
        output_tokens=0 if tokens is not None else None,
        total_tokens=tokens,
        cost_usd=cost,
    )


def _result(
    scenario: str,
    model: str,
    passed: int,
    total: int,
    *,
    category: str = CATEGORY_HAPPY_PATH,
    tokens: int | None = 100,
    cost: float | None = 0.01,
    duration: float | None = 2.0,
    exercised: bool = True,
) -> ScenarioResult:
    runs = tuple(
        RunOutcome(
            passed=index < passed,
            usage=_usage(tokens, cost),
            duration_s=duration,
            exercised=exercised,
        )
        for index in range(total)
    )
    return ScenarioResult(
        scenario=scenario,
        role="agent",
        model=model,
        runs=runs,
        category=category,
        threshold=0.8 if category == CATEGORY_HAPPY_PATH else 1.0,
    )


def _scripted(
    table: dict[tuple[str, str], tuple[int, int]],
    **kwargs: Any,
) -> Callable[..., Awaitable[ScenarioResult]]:
    """A `run_case` answering from (model name, scenario name) -> (passed, runs)."""

    async def run_case(
        case: Any, *, model: BaseChatModel, model_name: str, runs: int
    ) -> ScenarioResult:
        passed, total = table.get((model_name, case.scenario.name), (runs, runs))
        return _result(case.scenario.name, model_name, passed, total, **kwargs)

    return run_case


def _candidates(*names: str) -> list[ModelCandidate]:
    return [
        ModelCandidate(name=name, model=FakeListChatModel(responses=["x"]))
        for name in names
    ]


async def test_matrix_has_one_cell_per_model_and_capability() -> None:
    matrix = await run_capability_matrix(
        _candidates("model-a", "model-b"),
        capabilities=["grounding", "memory"],
        runs=3,
        run_case=_scripted({}),
    )

    assert matrix.models == ("model-a", "model-b")
    assert matrix.columns == ("grounding", "memory")
    for model in matrix.models:
        for column in matrix.columns:
            assert matrix.cell(model, column) is not None


async def test_all_capabilities_by_default() -> None:
    matrix = await run_capability_matrix(
        _candidates("m"), runs=1, run_case=_scripted({})
    )

    assert matrix.columns == tuple(c.id for c in CAPABILITIES)


async def test_cell_aggregates_rate_gate_exercised_tokens_cost_duration() -> None:
    grounding = [c.scenario.name for c in load_capability_scenarios(["grounding"])]
    assert len(grounding) == 2
    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["grounding"],
        runs=4,
        run_case=_scripted({("m", grounding[0]): (4, 4), ("m", grounding[1]): (2, 4)}),
    )

    cell = matrix.cell("m", "grounding")
    assert cell.scenarios == 2
    assert cell.runs == 8
    assert cell.passed == 6
    assert cell.success_rate == pytest.approx(0.75)
    assert cell.exercised == 8
    assert cell.tokens == 800  # 100 tokens x 8 runs
    assert cell.cost_usd == pytest.approx(0.08)
    assert cell.duration_s == pytest.approx(16.0)
    # one scenario at 50% misses its 80% gate, so the cell misses its gate
    assert cell.gate_passed is False
    assert cell.failing == (grounding[1],)


async def test_gate_passes_when_every_scenario_meets_its_own() -> None:
    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["memory"],
        runs=5,
        run_case=_scripted({("m", "memory_client_id"): (4, 5)}),  # 80%
    )

    cell = matrix.cell("m", "memory")
    assert cell.gate_passed is True
    assert cell.failing == ()


async def test_guardrail_never_exercised_fails_the_cell() -> None:
    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["untrusted_input"],
        runs=3,
        run_case=_scripted({}, category=CATEGORY_GUARDRAIL, exercised=False),
    )

    cell = matrix.cell("m", "untrusted_input")
    assert cell.exercised == 0
    assert cell.gate_passed is False


async def test_unknown_usage_is_none_never_zero() -> None:
    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["memory"],
        runs=2,
        run_case=_scripted({}, tokens=None, cost=None, duration=None),
    )

    cell = matrix.cell("m", "memory")
    assert cell.tokens is None
    assert cell.cost_usd is None
    assert cell.duration_s is None


async def test_runs_default_to_eval_runs_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_RUNS", "2")
    seen: list[int] = []

    async def run_case(
        case: Any, *, model: BaseChatModel, model_name: str, runs: int
    ) -> ScenarioResult:
        seen.append(runs)
        return _result(case.scenario.name, model_name, runs, runs)

    await run_capability_matrix(
        _candidates("m"), capabilities=["memory"], run_case=run_case
    )

    assert seen == [2]


async def test_capability_missing_a_host_command_is_skipped_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["commands", "memory"],
        runs=1,
        run_case=_scripted({}),
    )

    skipped = matrix.cell("m", "commands")
    assert skipped.skipped is not None
    assert "bwrap" in skipped.skipped and "factor" in skipped.skipped
    assert skipped.runs == 0
    assert matrix.cell("m", "memory").skipped is None


async def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown capability"):
        await run_capability_matrix(
            _candidates("m"), capabilities=["nope"], runs=1, run_case=_scripted({})
        )


async def test_needs_at_least_one_model_and_unique_names() -> None:
    with pytest.raises(ValueError, match="at least one"):
        await run_capability_matrix([], runs=1, run_case=_scripted({}))
    with pytest.raises(ValueError, match="duplicate"):
        await run_capability_matrix(
            _candidates("m", "m"), runs=1, run_case=_scripted({})
        )


async def test_adopter_case_adds_a_column_to_the_same_matrix() -> None:
    calls: list[tuple[str, int]] = []

    async def adopter_run(
        model: BaseChatModel, model_name: str, runs: int
    ) -> ScenarioResult:
        calls.append((model_name, runs))
        return _result("my_scenario", model_name, runs, runs)

    matrix = await run_capability_matrix(
        _candidates("a", "b"),
        capabilities=["memory"],
        extra_cases=[MatrixCase(column="my-agent", run=adopter_run)],
        runs=3,
        run_case=_scripted({}),
    )

    assert matrix.columns == ("memory", "my-agent")
    assert calls == [("a", 3), ("b", 3)]
    assert matrix.cell("b", "my-agent").success_rate == 1.0


async def test_adopter_column_cannot_reuse_a_capability_id() -> None:
    async def run(model: BaseChatModel, model_name: str, runs: int) -> ScenarioResult:
        raise AssertionError("must not run")

    with pytest.raises(ValueError, match="memory"):
        await run_capability_matrix(
            _candidates("m"),
            capabilities=["memory"],
            extra_cases=[MatrixCase(column="memory", run=run)],
            runs=1,
            run_case=_scripted({}),
        )


async def test_scenario_case_runs_an_adopter_scenario_through_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(name="mine", role="agent", turns=())  # type: ignore[call-arg]
    captured: dict[str, Any] = {}

    async def fake_run_scenario(sc: Scenario, **kwargs: Any) -> ScenarioResult:
        captured.update(kwargs)
        captured["scenario"] = sc
        return _result(sc.name, kwargs["model_name"], kwargs["runs"], kwargs["runs"])

    monkeypatch.setattr(matrix_module, "run_scenario", fake_run_scenario)
    factory = object()
    case = scenario_case("mine", scenario, registry_factory=factory)  # type: ignore[arg-type]

    model = FakeListChatModel(responses=["x"])
    result = await case.run(model, "m", 2)

    assert case.column == "mine"
    assert captured["scenario"] is scenario
    assert captured["registry_factory"] is factory
    assert captured["runs"] == 2 and captured["model"] is model
    assert result.success_rate == 1.0


async def test_default_runner_uses_the_capability_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_run(case: Any, **kwargs: Any) -> ScenarioResult:
        seen.append(case.capability.id)
        return _result(case.scenario.name, kwargs["model_name"], 1, 1)

    monkeypatch.setattr(matrix_module, "run_capability_scenario", fake_run)

    matrix = await run_capability_matrix(
        _candidates("m"), capabilities=["memory"], runs=1
    )

    assert seen == ["memory"]
    assert matrix.cell("m", "memory").runs == 1


async def _matrix() -> CapabilityMatrix:
    return await run_capability_matrix(
        _candidates("model-a", "model-b"),
        capabilities=["memory", "grounding"],
        runs=2,
        run_case=_scripted({("model-b", "memory_client_id"): (0, 2)}),
    )


async def test_markdown_is_a_model_by_capability_table() -> None:
    markdown = (await _matrix()).to_markdown()
    lines = markdown.splitlines()

    header = next(line for line in lines if line.startswith("| Model |"))
    assert header.split("|")[2].strip() == "memory"
    assert "grounding" in header
    row_b = next(line for line in lines if line.startswith("| model-b |"))
    assert "0%" in row_b and "FAIL" in row_b
    row_a = next(line for line in lines if line.startswith("| model-a |"))
    assert "100%" in row_a and "PASS" in row_a
    # a cell is a rate for one model on a generic prompt and a minimal fixture
    assert "not a verdict" in markdown
    # the second table carries exercised runs, tokens, cost and duration
    assert "Tokens" in markdown and "Cost (USD)" in markdown
    assert "Duration (s)" in markdown and "Exercised" in markdown


async def test_markdown_shows_n_a_for_unknown_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    matrix = await run_capability_matrix(
        _candidates("m"),
        capabilities=["commands", "memory"],
        runs=1,
        run_case=_scripted({}, tokens=None, cost=None, duration=None),
    )

    markdown = matrix.to_markdown()

    assert "skipped" in markdown
    assert "n/a" in markdown


async def test_to_dict_is_json_serializable_and_complete() -> None:
    data = (await _matrix()).to_dict()

    parsed = json.loads(json.dumps(data))
    assert parsed["models"] == ["model-a", "model-b"]
    assert parsed["columns"] == ["memory", "grounding"]
    cell = next(
        c
        for c in parsed["cells"]
        if c["model"] == "model-b" and c["column"] == "memory"
    )
    for key in (
        "scenarios",
        "runs",
        "passed",
        "success_rate",
        "exercised",
        "gate_passed",
        "tokens",
        "cost_usd",
        "duration_s",
        "skipped",
        "failing",
    ):
        assert key in cell
    assert cell["gate_passed"] is False


async def test_write_matrix_writes_json_and_markdown(tmp_path: pathlib.Path) -> None:
    import datetime

    matrix = await _matrix()
    now = datetime.datetime(2026, 10, 4, 12, 0, 0, tzinfo=datetime.UTC)

    json_path, md_path = write_matrix(matrix, out_dir=tmp_path / "res", now=now)

    assert json_path.name == "capability-matrix-20261004T120000Z.json"
    assert md_path.name == "capability-matrix-20261004T120000Z.md"
    assert json.loads(json_path.read_text())["models"] == ["model-a", "model-b"]
    assert md_path.read_text().startswith("# Capability matrix")


async def test_write_matrix_never_overwrites_a_same_second_export(
    tmp_path: pathlib.Path,
) -> None:
    import datetime

    first = await _matrix()
    second = await _matrix()
    now = datetime.datetime(2026, 10, 4, 12, 0, 0, tzinfo=datetime.UTC)
    out = tmp_path / "res"

    json_1, md_1 = write_matrix(first, out_dir=out, now=now)
    first_json = json_1.read_text()
    json_2, md_2 = write_matrix(second, out_dir=out, now=now)

    assert json_1 != json_2
    assert md_1 != md_2
    assert json_1.read_text() == first_json
    assert json_2.stem == md_2.stem  # a pair shares one stem
    assert len(list(out.iterdir())) == 4


def test_candidates_from_names_builds_one_model_per_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str | None] = []

    def fake_build(model_override: str | None = None) -> tuple[BaseChatModel, str]:
        built.append(model_override)
        return FakeListChatModel(responses=["x"]), f"resolved-{model_override}"

    monkeypatch.setattr(matrix_module, "build_eval_model", fake_build)

    candidates = matrix_module.candidates_from_names(["a/one", "b/two"])

    assert built == ["a/one", "b/two"]
    # the name the adopter typed is the name reported, not a provider echo
    assert [c.name for c in candidates] == ["a/one", "b/two"]


def _load_script() -> Any:
    import importlib.util

    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "scripts"
        / ("run-capability-matrix.py")
    )
    spec = importlib.util.spec_from_file_location("run_capability_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_parses_models_capabilities_runs_and_out_dir() -> None:
    script = _load_script()

    args = script.parse_args(
        [
            "--model", "a/one", "--model", "b/two",
            "--capability", "memory", "--capability", "grounding",
            "--runs", "2", "--out-dir", "somewhere",
        ]
    )  # fmt: skip

    assert args.model == ["a/one", "b/two"]
    assert args.capability == ["memory", "grounding"]
    assert args.runs == 2
    assert args.out_dir == pathlib.Path("somewhere")


def test_script_requires_at_least_one_model() -> None:
    script = _load_script()

    with pytest.raises(SystemExit):
        script.parse_args([])


async def test_script_runs_the_matrix_and_writes_both_files(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()
    monkeypatch.setattr(
        script, "candidates_from_names", lambda names: _candidates(*names)
    )

    async def fake_run(candidates: Any, **kwargs: Any) -> CapabilityMatrix:
        return await run_capability_matrix(candidates, run_case=_scripted({}), **kwargs)

    monkeypatch.setattr(script, "run_capability_matrix", fake_run)

    code = await script.amain(
        ["--model", "m", "--capability", "memory", "--runs", "1",
         "--out-dir", str(tmp_path)]
    )  # fmt: skip

    assert code == 0
    assert len(list(tmp_path.glob("capability-matrix-*.json"))) == 1
    assert len(list(tmp_path.glob("capability-matrix-*.md"))) == 1
    assert "| Model |" in capsys.readouterr().out


async def test_repeated_capability_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate capability"):
        await run_capability_matrix(
            _candidates("m"),
            capabilities=["grounding", "grounding"],
            runs=1,
            run_case=_scripted({}),
        )


def test_candidates_from_names_closes_earlier_clients_when_the_nth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    def fake_build(model_override: str | None = None) -> tuple[BaseChatModel, str]:
        if model_override == "bad":
            raise RuntimeError("no key for bad")
        model = FakeListChatModel(responses=["x"])
        object.__setattr__(model, "http_async_client", _Client(str(model_override)))
        return model, str(model_override)

    monkeypatch.setattr(matrix_module, "build_eval_model", fake_build)

    with pytest.raises(RuntimeError, match="no key for bad"):
        matrix_module.candidates_from_names(["a", "b", "bad", "c"])

    assert closed == ["a", "b"]


async def test_candidates_from_names_closes_earlier_clients_inside_a_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class _Client:
        async def aclose(self) -> None:
            closed.append("a")

    def fake_build(model_override: str | None = None) -> tuple[BaseChatModel, str]:
        if model_override == "bad":
            raise RuntimeError("boom")
        model = FakeListChatModel(responses=["x"])
        object.__setattr__(model, "http_async_client", _Client())
        return model, "a"

    monkeypatch.setattr(matrix_module, "build_eval_model", fake_build)

    with pytest.raises(RuntimeError, match="boom"):
        matrix_module.candidates_from_names(["a", "bad"])

    assert closed == ["a"]
