"""Translate public terminal bindings into local presentation eligibility."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from regie.constants import REGIE_STAGEABLE_PROVIDER_KIND
from regie.contracts import PresentationOperations, PresentationTarget
from theater.frontend import Participant, Provider


@dataclass(frozen=True, slots=True)
class PresentationEligibility:
    target: PresentationTarget | None
    allowed: bool
    reason: str | None = None


def target_for_participant(
    participant: Participant, providers: Mapping[str, Provider]
) -> PresentationTarget | None:
    """Build a target solely from the public route identity and provider kind."""
    route = participant.terminal_route
    if route is None:
        return None
    identity = route.identity
    provider = providers.get(identity.provider_id)
    if provider is None:
        return None
    return PresentationTarget(
        provider_id=identity.provider_id,
        provider_kind=provider.kind,
        terminal_id=identity.terminal_id,
        terminal_incarnation=identity.terminal_incarnation,
        occupant=dict(identity.occupant),
    )


def stageability(
    participant: Participant,
    providers: Mapping[str, Provider],
    ops: PresentationOperations,
) -> PresentationEligibility:
    """Expose a non-tmux route as visible but explicitly unstageable."""
    target = target_for_participant(participant, providers)
    if target is None:
        return PresentationEligibility(None, False, "no public terminal route is available")
    if target.provider_kind != REGIE_STAGEABLE_PROVIDER_KIND:
        return PresentationEligibility(
            target,
            False,
            f"terminal provider {target.provider_kind!r} cannot be staged in Régie",
        )
    try:
        allowed, reason = ops.can_stage(target)
    except Exception as exc:
        return PresentationEligibility(target, False, f"local presentation check failed: {exc}")
    return PresentationEligibility(target, allowed, reason)


__all__ = ["PresentationEligibility", "stageability", "target_for_participant"]
