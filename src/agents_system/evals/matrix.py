"""The per-model capability matrix (#228, ADR-010 part 4).

Given candidate models and the capabilities an adopter's agents need (all by
default), `run_capability_matrix` runs each capability's scenarios `EVAL_RUNS`
times per model and returns a model x capability table: success rate against
the gate, exercised runs, tokens, cost and duration. Every number comes from
`ScenarioResult`; the matrix only aggregates.

A cell is a rate for ONE model on the kit's generic prompt and a minimal
fixture agent. It is not a verdict on an adopter's own agent: use the matrix
to compare candidate models, then run your own scenarios on your own agent.
Those join the same matrix through ``extra_cases`` (see `scenario_case`).

Run it from Python::

    from agents_system.evals.matrix import ModelCandidate, run_capability_matrix

    matrix = await run_capability_matrix(
        [ModelCandidate("model-a", model_a), ModelCandidate("model-b", model_b)],
        capabilities=["grounding", "memory"],
        runs=3,
    )
    print(matrix.to_markdown())

or with ``scripts/run-capability-matrix.py``. See `docs/platform/live-eval.md`.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import pathlib
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel

from agents_system.agent.spec import Agent
from agents_system.evals.capabilities import (
    CAPABILITIES,
    CapabilityScenario,
    get_capability,
    load_capability_scenarios,
    missing_requirements,
    run_capability_scenario,
)
from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import DEFAULT_RESULTS_DIR
from agents_system.evals.runner import ScenarioResult, run_scenario
from agents_system.evals.schema import Scenario
from agents_system.harness.loader import RootConfig
from agents_system.harness.registry import ToolRegistry

#: Trials per scenario when neither ``runs`` nor ``EVAL_RUNS`` is given; the
#: same default every live module uses.
DEFAULT_MATRIX_RUNS = 3

_NA = "n/a"


@dataclasses.dataclass(frozen=True)
class ModelCandidate:
    """A model to measure and the name to report it under."""

    name: str
    model: BaseChatModel


@dataclasses.dataclass(frozen=True)
class MatrixCase:
    """An extra column: runs its scenarios against one candidate model.

    ``run(model, model_name, runs)`` returns the column's `ScenarioResult`;
    build one for a YAML scenario on your own agent with `scenario_case`.
    """

    column: str
    run: Callable[[BaseChatModel, str, int], Awaitable[ScenarioResult]]


def candidates_from_names(names: Iterable[str]) -> list[ModelCandidate]:
    """One `ModelCandidate` per model name, built from the configured eval
    provider (``EVAL_PROVIDER`` and that provider's variables) with the name
    as the model override. The name you typed is the name reported."""
    return [
        ModelCandidate(name=name, model=build_eval_model(model_override=name)[0])
        for name in names
    ]


def scenario_case(
    column: str,
    scenario: Scenario,
    *,
    registry: ToolRegistry | None = None,
    registry_factory: Callable[[], ToolRegistry] | None = None,
    roots: RootConfig | None = None,
    agent: Agent | None = None,
    agents_root: pathlib.Path | None = None,
) -> MatrixCase:
    """A `MatrixCase` running *scenario* on your own agent through
    `run_scenario`; the keyword arguments are `run_scenario`'s."""

    async def run(model: BaseChatModel, model_name: str, runs: int) -> ScenarioResult:
        return await run_scenario(
            scenario,
            model=model,
            model_name=model_name,
            registry=registry,
            registry_factory=registry_factory,
            roots=roots,
            runs=runs,
            agent=agent,
            agents_root=agents_root,
        )

    return MatrixCase(column=column, run=run)


@dataclasses.dataclass(frozen=True)
class MatrixCell:
    """One model on one capability (or adopter column), over all its
    scenarios."""

    model: str
    column: str
    scenarios: int
    #: Total runs across the cell's scenarios, and how many passed.
    runs: int
    passed: int
    #: Runs that put a guardrail assertion to the test (see
    #: `AssertionOutcome.exercised`).
    exercised: int
    #: ``passed / runs``; 0.0 for a skipped cell.
    success_rate: float
    #: True only when EVERY scenario meets its own gate (80% happy path,
    #: 100% of exercised runs for a guardrail). False for a skipped cell.
    gate_passed: bool
    #: ``None`` when any run did not report it: never a partial sum.
    tokens: int | None
    cost_usd: float | None
    duration_s: float | None
    #: Why the cell did not run (missing host commands), else ``None``.
    skipped: str | None = None
    #: Scenarios that missed their gate.
    failing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["failing"] = list(self.failing)
        return data


def _sum_known(values: Iterable[int | float | None]) -> int | float | None:
    collected = list(values)
    if not collected or any(value is None for value in collected):
        return None
    return sum(value for value in collected if value is not None)


def _build_cell(
    model: str, column: str, results: Sequence[ScenarioResult]
) -> MatrixCell:
    runs = sum(len(result.runs) for result in results)
    passed = sum(1 for result in results for run in result.runs if run.passed)
    usages = [result.total_usage for result in results]
    tokens = _sum_known(u.total_tokens if u else None for u in usages)
    cost = _sum_known(u.cost_usd if u else None for u in usages)
    duration = _sum_known(result.total_duration_s for result in results)
    failing = tuple(result.scenario for result in results if not result.gate.passed)
    return MatrixCell(
        model=model,
        column=column,
        scenarios=len(results),
        runs=runs,
        passed=passed,
        exercised=sum(result.exercised_count for result in results),
        success_rate=passed / runs if runs else 0.0,
        gate_passed=bool(results) and not failing,
        tokens=None if tokens is None else int(tokens),
        cost_usd=None if cost is None else float(cost),
        duration_s=None if duration is None else float(duration),
        failing=failing,
    )


def _skipped_cell(model: str, column: str, reason: str) -> MatrixCell:
    return MatrixCell(
        model=model,
        column=column,
        scenarios=0,
        runs=0,
        passed=0,
        exercised=0,
        success_rate=0.0,
        gate_passed=False,
        tokens=None,
        cost_usd=None,
        duration_s=None,
        skipped=reason,
    )


@dataclasses.dataclass(frozen=True)
class CapabilityMatrix:
    """A model x capability table, plus adopter columns."""

    models: tuple[str, ...]
    columns: tuple[str, ...]
    cells: tuple[MatrixCell, ...]
    runs: int

    def cell(self, model: str, column: str) -> MatrixCell:
        for cell in self.cells:
            if cell.model == model and cell.column == column:
                return cell
        raise KeyError(f"no cell for model {model!r}, column {column!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": list(self.models),
            "columns": list(self.columns),
            "runs_per_scenario": self.runs,
            "cells": [cell.to_dict() for cell in self.cells],
        }

    def to_markdown(self, *, title: str = "Capability matrix") -> str:
        summary = [
            f"| Model | {' | '.join(self.columns)} |",
            f"|---|{'---|' * len(self.columns)}",
        ]
        for model in self.models:
            entries = []
            for column in self.columns:
                cell = self.cell(model, column)
                if cell.skipped is not None:
                    entries.append(f"skipped ({cell.skipped})")
                else:
                    gate = "PASS" if cell.gate_passed else "FAIL"
                    entries.append(
                        f"{cell.success_rate:.0%} ({cell.passed}/{cell.runs}) {gate}"
                    )
            summary.append(f"| {model} | {' | '.join(entries)} |")

        detail = [
            (
                "| Model | Capability | Scenarios | Runs | Exercised | Success rate "
                "| Gate | Tokens | Cost (USD) | Duration (s) |"
            ),
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for cell in self.cells:
            if cell.skipped is not None:
                detail.append(
                    f"| {cell.model} | {cell.column} | 0 | 0 | 0 | {_NA} | "
                    f"skipped | {_NA} | {_NA} | {_NA} |"
                )
                continue
            tokens = _NA if cell.tokens is None else str(cell.tokens)
            cost = _NA if cell.cost_usd is None else f"{cell.cost_usd:.4f}"
            duration = _NA if cell.duration_s is None else f"{cell.duration_s:.2f}"
            gate = "PASS" if cell.gate_passed else "FAIL"
            detail.append(
                f"| {cell.model} | {cell.column} | {cell.scenarios} | {cell.runs} "
                f"| {cell.exercised} | {cell.success_rate:.0%} | {gate} | {tokens} "
                f"| {cost} | {duration} |"
            )

        failing = [
            f"- {cell.model} / {cell.column}: {', '.join(cell.failing)}"
            for cell in self.cells
            if cell.failing
        ]
        lines = [
            f"# {title}",
            "",
            (
                f"{self.runs} run(s) per scenario. Each cell is the share of runs "
                "that passed, and whether every scenario in it met its own gate "
                "(80% happy path; 100% of exercised runs for a guardrail)."
            ),
            "",
            *summary,
            "",
            "## Detail",
            "",
            *detail,
            "",
        ]
        if failing:
            lines += ["## Scenarios that missed their gate", "", *failing, ""]
        lines += [
            (
                "A cell is a rate for one model on the kit's generic prompt and a "
                "minimal fixture agent, not a verdict on your own agent."
            ),
            "",
        ]
        return "\n".join(lines)


RunCase = Callable[..., Awaitable[ScenarioResult]]


def _resolve_runs(runs: int | None) -> int:
    if runs is None:
        runs = int(os.environ.get("EVAL_RUNS", str(DEFAULT_MATRIX_RUNS)))
    if runs < 1:
        raise ValueError(f"runs must be at least 1, got {runs}")
    return runs


async def run_capability_matrix(
    candidates: Sequence[ModelCandidate],
    *,
    capabilities: Iterable[str] | None = None,
    extra_cases: Sequence[MatrixCase] = (),
    runs: int | None = None,
    run_case: RunCase | None = None,
) -> CapabilityMatrix:
    """Run every selected capability scenario *runs* times on every
    candidate model and tabulate the results.

    *capabilities* are ids (every capability when ``None``); *runs* defaults
    to ``EVAL_RUNS``, else 3. *extra_cases* add columns for your own
    scenarios. A capability whose host commands are missing is skipped, with
    the reason in its cells. *run_case* replaces `run_capability_scenario`
    (tests inject scripted results through it); it is called as
    ``run_case(case, model=..., model_name=..., runs=...)``.
    """
    candidate_list = list(candidates)
    if not candidate_list:
        raise ValueError("a capability matrix needs at least one model candidate")
    names = [candidate.name for candidate in candidate_list]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate model name(s): {', '.join(duplicates)}")

    selected = (
        list(CAPABILITIES)
        if capabilities is None
        else [get_capability(capability_id) for capability_id in capabilities]
    )
    ids = {capability.id for capability in selected}
    for extra in extra_cases:
        if extra.column in ids:
            raise ValueError(
                f"extra column {extra.column!r} collides with the capability "
                f"of the same id; pick another name"
            )
        ids.add(extra.column)

    trials = _resolve_runs(runs)
    runner: RunCase = run_case or run_capability_scenario
    cells: list[MatrixCell] = []
    for candidate in candidate_list:
        for capability in selected:
            missing = missing_requirements(capability)
            if missing:
                reason = f"needs {', '.join(missing)} on this host"
                cells.append(_skipped_cell(candidate.name, capability.id, reason))
                continue
            cases: tuple[CapabilityScenario, ...] = load_capability_scenarios(
                [capability.id]
            )
            results = [
                await runner(
                    case,
                    model=candidate.model,
                    model_name=candidate.name,
                    runs=trials,
                )
                for case in cases
            ]
            cells.append(_build_cell(candidate.name, capability.id, results))
        for extra in extra_cases:
            result = await extra.run(candidate.model, candidate.name, trials)
            cells.append(_build_cell(candidate.name, extra.column, [result]))

    return CapabilityMatrix(
        models=tuple(names),
        columns=tuple(capability.id for capability in selected)
        + tuple(extra.column for extra in extra_cases),
        cells=tuple(cells),
        runs=trials,
    )


def write_matrix(
    matrix: CapabilityMatrix,
    *,
    out_dir: pathlib.Path = DEFAULT_RESULTS_DIR,
    now: datetime.datetime | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write *matrix* as one timestamped JSON file plus a Markdown table
    under *out_dir* (default ``evals/results/``, gitignored). Returns
    ``(json_path, markdown_path)``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"capability-matrix-{stamp}.json"
    json_path.write_text(
        json.dumps(matrix.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path = out_dir / f"capability-matrix-{stamp}.md"
    markdown_path.write_text(
        matrix.to_markdown(title=f"Capability matrix -- {stamp}"), encoding="utf-8"
    )
    return json_path, markdown_path


__all__ = [
    "DEFAULT_MATRIX_RUNS",
    "CapabilityMatrix",
    "MatrixCase",
    "MatrixCell",
    "ModelCandidate",
    "candidates_from_names",
    "run_capability_matrix",
    "scenario_case",
    "write_matrix",
]
