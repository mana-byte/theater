"""Participant-aware model and reasoning allowlist policy."""

from __future__ import annotations

from theater.daemon.rails import check_model_allowed, check_reasoning_allowed


def validate_settings(
    config, harness: str, model: str | None, reasoning_effort: str | None
) -> None:
    check_model_allowed(harness, model, config.models_for(harness))
    check_reasoning_allowed(harness, reasoning_effort, config.reasoning_for(harness))
