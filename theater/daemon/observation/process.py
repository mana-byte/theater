"""Process facts for transcript observation, independent of control routing."""

from __future__ import annotations

from dataclasses import dataclass

from theater.models import Participant, Status


@dataclass(frozen=True, slots=True)
class ObservationProcess:
    """The process identity supplied when an observation source was opened."""

    pid: int
    terminal_fence: tuple[str, int, str, str] | None = None
    started_at: object = None


def observation_process(store, participant: Participant) -> ObservationProcess | None:
    """Prefer the provider's fenced occupant over the legacy registration PID."""
    if participant.status is Status.DEAD:
        return None
    repository = getattr(store, "terminal_bindings", None)
    binding = repository.get(participant.id) if repository is not None else None
    if binding is None:
        return ObservationProcess(participant.live_pid) if participant.live_pid else None
    provider = store.providers.get(binding.provider_id)
    if (
        binding.health != "healthy"
        or provider is None
        or provider.generation != binding.provider_generation
        or binding.process_facts is None
    ):
        return None
    pid = binding.process_facts.get("pid")
    if type(pid) is not int or pid <= 0:
        return None
    return ObservationProcess(
        pid=pid,
        terminal_fence=(
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
        ),
        started_at=binding.process_facts.get("started_at"),
    )


def observation_process_id(store, participant: Participant) -> int | None:
    process = observation_process(store, participant)
    return process.pid if process is not None else None
