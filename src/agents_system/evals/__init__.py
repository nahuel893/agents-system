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
    GUARDRAIL_THRESHOLD,
    AssertionFailure,
    AssertionOutcome,
    RunOutcome,
    ScenarioGate,
    ScenarioResult,
    evaluate_assertions,
    run_scenario,
)
from agents_system.evals.schema import (
    CATEGORY_GUARDRAIL,
    CATEGORY_HAPPY_PATH,
    DEFAULT_HAPPY_PATH_THRESHOLD,
    Scenario,
    ScenarioAssertions,
    ScenarioError,
    load_scenario,
    load_scenarios,
)

__all__ = [
    "CATEGORY_GUARDRAIL",
    "CATEGORY_HAPPY_PATH",
    "DEFAULT_HAPPY_PATH_THRESHOLD",
    "DEFAULT_RESULTS_DIR",
    "GUARDRAIL_THRESHOLD",
    "AssertionFailure",
    "AssertionOutcome",
    "RunOutcome",
    "Scenario",
    "ScenarioAssertions",
    "ScenarioError",
    "ScenarioGate",
    "ScenarioResult",
    "build_eval_model",
    "build_live_registry",
    "build_live_registry_factory",
    "evaluate_assertions",
    "load_scenario",
    "load_scenarios",
    "model_display_name",
    "run_scenario",
    "write_results",
]
