"""Checks a load run makes on the metrics it scraped."""

from __future__ import annotations

from agents_system.evals.live_harness import metric_value


def model_reach_failure(
    metrics_text: str, *, runtime_id: str, conversations: int
) -> str | None:
    """Why a load run did not exercise the model, or ``None`` when it did.

    A turn the provider rejects still sends one message, the fallback reply,
    so counting replies cannot tell a run that reached the model from one
    that never did (#348). The metrics can: a run reached the model when the
    provider reported input tokens, or, for a provider that reports no
    usage, when at least one turn ended ``ok``.
    """
    input_tokens = metric_value(
        metrics_text, "agent_tokens_total", runtime_id=runtime_id, direction="input"
    )
    if input_tokens:
        return None
    ok_turns = metric_value(
        metrics_text, "agent_turns_total", runtime_id=runtime_id, outcome="ok"
    )
    if ok_turns:
        return None
    error_turns = metric_value(
        metrics_text, "agent_turns_total", runtime_id=runtime_id, outcome="error"
    )
    return (
        f"none of {conversations} conversations reached the model: "
        f"input_tokens={input_tokens}, ok_turns={ok_turns}, "
        f"error_turns={error_turns}. Check the provider key and the model name."
    )
