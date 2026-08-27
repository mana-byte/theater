"""Capability checks and the plan_launch compatibility funnel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from theater.harness.contracts.harness import Harness
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.registry.lookup import get
from theater.models import BadRequest


def supports_model(harness: Harness) -> bool:
    """Whether this adapter can honour a ``model`` request."""
    return harness.launch_parameter_support.model


def check_model(harness: str, model: str | None) -> None:
    """Raise if this harness cannot honour a model request."""
    if model is not None and not supports_model(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support model selection")


def supports_reasoning(harness: Harness) -> bool:
    """Whether this adapter can honour a reasoning-effort request."""
    return harness.launch_parameter_support.reasoning_effort


def check_reasoning(harness: str, reasoning_effort: str | None) -> None:
    """Raise if this harness cannot honour a reasoning-effort request."""
    if reasoning_effort is not None and not supports_reasoning(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support reasoning effort selection")


def supports_resume(harness: Harness) -> bool:
    """Whether this adapter can honour a ``resume`` request."""
    return harness.launch_parameter_support.resume


def check_resume(harness: str, resume: str | None) -> None:
    """Raise if this harness cannot honour a resume request."""
    if resume is not None and not supports_resume(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support resume")


def plan_launch(
    harness: str,
    *,
    participant_id: str,
    prompt: str,
    config_path: Path,
    approval: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    resume: str | None = None,
) -> LaunchPlan:
    """The one funnel every spawn goes through, and so the one compat seam.

    ``model``, ``reasoning_effort``, and ``resume`` are each forwarded only
    when the caller named one.  Those are refused here, with a message that
    names the harness, rather than surfacing as a ``TypeError`` from inside
    the plugin or a child that silently came up on the wrong model.
    """
    found = get(harness)
    check_model(harness, model)
    check_reasoning(harness, reasoning_effort)
    check_resume(harness, resume)
    extra: dict[str, Any] = {}
    if model is not None:
        extra["model"] = model
    if reasoning_effort is not None:
        extra["reasoning_effort"] = reasoning_effort
    if resume is not None:
        extra["resume"] = resume
    return found.plan_launch(
        participant_id=participant_id,
        prompt=prompt,
        config_path=config_path,
        approval=approval,
        **extra,
    )
