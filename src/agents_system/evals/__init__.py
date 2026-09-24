"""Live-eval pipeline (#169, ADR-002 E.18).

Loads YAML scenarios, runs a scenario's role N times against a real model
through the platform's own `resolve()`/`build_runtime()` path, and writes
JSON + markdown results. See `docs/platform/live-eval.md`.
"""

from agents_system.evals.live_registry import (
    build_live_registry,
    build_live_registry_factory,
)
from agents_system.evals.provider import build_eval_model, model_display_name
from agents_system.evals.reporting import DEFAULT_RESULTS_DIR, write_results
from agents_system.evals.runner import (
    AssertionFailure,
    AssertionOutcome,
    RunOutcome,
    ScenarioResult,
    evaluate_assertions,
    run_scenario,
)
from agents_system.evals.schema import (
    Scenario,
    ScenarioAssertions,
    ScenarioError,
    load_scenario,
    load_scenarios,
)

__all__ = [
    "build_live_registry",
    "build_live_registry_factory",
    "build_eval_model",
    "model_display_name",
    "DEFAULT_RESULTS_DIR",
    "write_results",
    "AssertionFailure",
    "AssertionOutcome",
    "RunOutcome",
    "ScenarioResult",
    "evaluate_assertions",
    "run_scenario",
    "Scenario",
    "ScenarioAssertions",
    "ScenarioError",
    "load_scenario",
    "load_scenarios",
]
