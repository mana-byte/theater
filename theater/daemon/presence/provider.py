"""Provider-backed presence inspection with exact terminal fencing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import Participant, TerminalBindingRecord

logger = logging.getLogger("theater.daemon.presence")


@dataclass(frozen=True, slots=True)
class ProviderExitEvidence:
    """One identity-fenced provider observation that proves terminal exit."""

    participant_id: str
    provider_id: str
    provider_generation: int
    terminal_id: str
    terminal_incarnation: str
    occupant_evidence: Mapping[str, object]
    process_facts: Mapping[str, object] | None
    report_revision: int
    presence_revision: int
    lifecycle: Mapping[str, object]

    def matches(self, binding: TerminalBindingRecord) -> bool:
        return (
            self.participant_id == binding.participant_id
            and self.provider_id == binding.provider_id
            and self.provider_generation == binding.provider_generation
            and self.terminal_id == binding.terminal_id
            and self.terminal_incarnation == binding.terminal_incarnation
            and self.occupant_evidence == binding.occupant_evidence
            and (binding.process_facts is None or self.process_facts == binding.process_facts)
        )


ExitHandler = Callable[[ProviderExitEvidence], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class _Observation:
    binding_key: tuple[object, ...]
    state: PresenceState
    reason: str
    presence_revision: int
    observed_at: float
    observed_mono: float
    screen: str | None


class ProviderPresenceSource:
    """Cache current-generation inspect evidence for provider-bound terminals."""

    def __init__(
        self,
        registry: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        refresh_timeout: float,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._wall_clock = wall_clock
        self._refresh_timeout = refresh_timeout
        self._terminal_service: Any | None = None
        self._exit_handler: ExitHandler | None = None
        self._observations: dict[str, _Observation] = {}
        self._epochs: dict[str, int] = {}

    def invalidate(self, provider_id: str, generation: int) -> tuple[str, ...] | None:
        service = self._terminal_service
        if service is None or not service.connections.is_current(provider_id, generation):
            return None
        self._epochs[provider_id] = self._epochs.get(provider_id, 0) + 1
        invalidated = []
        for participant_id, observation in self._observations.items():
            if observation.binding_key[:2] == (provider_id, generation):
                self._observations[participant_id] = replace(
                    observation,
                    state=PresenceState.UNKNOWN,
                    reason="provider-presence-invalidated",
                )
                invalidated.append(participant_id)
        return tuple(invalidated)

    def configure(self, terminal_service: Any, *, exit_handler: ExitHandler | None = None) -> None:
        self._terminal_service = terminal_service
        self._exit_handler = exit_handler
        self._observations.clear()

    def observed_at_values(self) -> tuple[float, ...]:
        """Return cached audit timestamps without exposing mutable observations."""
        return tuple(observation.observed_at for observation in self._observations.values())

    def binding(self, participant_id: str) -> TerminalBindingRecord | None:
        store = getattr(self._registry, "store", None)
        repository = getattr(store, "terminal_bindings", None)
        if repository is None:
            return None
        return repository.get(participant_id)

    def has_binding(self, participant_id: str) -> bool:
        try:
            return self.binding(participant_id) is not None
        except Exception:
            logger.warning("provider binding lookup failed for %s", participant_id, exc_info=True)
            return True

    def snapshot(
        self,
        participant_id: str,
        *,
        revision: int,
        stale_after: float,
    ) -> PresenceSnapshot | None:
        try:
            binding = self.binding(participant_id)
        except Exception:
            logger.warning("provider binding lookup failed for %s", participant_id, exc_info=True)
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "provider-binding-unavailable", revision, None
            )
        return self.snapshot_for_binding(
            participant_id,
            binding,
            revision=revision,
            stale_after=stale_after,
        )

    def snapshot_for_binding(
        self,
        participant_id: str,
        binding: TerminalBindingRecord | None,
        *,
        revision: int,
        stale_after: float,
        allow_reconciling: bool = False,
    ) -> PresenceSnapshot | None:
        """Project cached evidence against a caller's transaction-local binding."""
        if binding is None:
            return None
        observation = self._observations.get(participant_id)
        if (
            observation is not None
            and observation.binding_key == self._binding_key(binding)
            and observation.reason == "terminal-exited"
        ):
            return PresenceSnapshot(
                PresenceState.ABSENT,
                observation.reason,
                revision,
                observation.observed_at,
            )
        service = self._terminal_service
        if service is None:
            return PresenceSnapshot(PresenceState.UNKNOWN, "provider-not-composed", revision, None)
        if not service.connections.is_current(binding.provider_id, binding.provider_generation):
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "provider-generation-stale", revision, None
            )
        health = service.connections.health(binding.provider_id)
        if health != "online" and not (allow_reconciling and health == "reconciling"):
            return PresenceSnapshot(PresenceState.UNKNOWN, f"provider-{health}", revision, None)
        if binding.health != "healthy":
            return PresenceSnapshot(
                PresenceState.UNKNOWN, f"terminal-{binding.health}", revision, None
            )
        if observation is None:
            return PresenceSnapshot(PresenceState.UNKNOWN, "provider-not-observed", revision, None)
        if observation.binding_key != self._binding_key(binding):
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "terminal-binding-changed", revision, None
            )
        if self._clock() - observation.observed_mono > stale_after:
            return PresenceSnapshot(
                PresenceState.UNKNOWN,
                "provider-evidence-stale",
                revision,
                observation.observed_at,
            )
        return PresenceSnapshot(
            observation.state,
            observation.reason,
            revision,
            observation.observed_at,
        )

    def screen(self, participant_id: str, *, stale_after: float) -> str | None:
        try:
            binding = self.binding(participant_id)
        except Exception:
            return None
        service = self._terminal_service
        observation = self._observations.get(participant_id)
        if (
            binding is None
            or service is None
            or not service.connections.is_current(binding.provider_id, binding.provider_generation)
            or service.connections.health(binding.provider_id) != "online"
            or binding.health != "healthy"
            or observation is None
            or observation.binding_key != self._binding_key(binding)
            or self._clock() - observation.observed_mono > stale_after
        ):
            return None
        return observation.screen

    async def refresh(
        self, participants: Sequence[Participant], *, screen_max_bytes: int = 0
    ) -> bool:
        bound = []
        lookup_failed = False
        for participant in participants:
            try:
                bound.append((participant.id, self.binding(participant.id)))
            except Exception:
                lookup_failed = True
                logger.warning(
                    "provider binding lookup failed for %s", participant.id, exc_info=True
                )
        targets = [(participant_id, binding) for participant_id, binding in bound if binding]
        if not targets:
            return lookup_failed
        await asyncio.gather(
            *(
                self._refresh_one(participant_id, binding, screen_max_bytes=screen_max_bytes)
                for participant_id, binding in targets
            )
        )
        return True

    async def _refresh_one(
        self,
        participant_id: str,
        binding: TerminalBindingRecord,
        *,
        screen_max_bytes: int = 0,
    ) -> None:
        epoch = self._epochs.get(binding.provider_id, 0)
        service = self._terminal_service
        if service is None:
            self._unknown(participant_id, binding, "provider-not-composed")
            return
        if not service.connections.is_current(binding.provider_id, binding.provider_generation):
            generation = service.connections.current_generation(binding.provider_id)
            if generation is None:
                self._unknown(participant_id, binding, "provider-generation-stale")
                return
            # Only the provider's exact inspect result may restore this older binding.
            binding = replace(binding, provider_generation=generation)
        health = service.connections.health(binding.provider_id)
        if health != "online":
            self._unknown(participant_id, binding, f"provider-{health}")
            return
        try:
            async with asyncio.timeout(self._refresh_timeout):
                result = await service.inspect(
                    binding.provider_id,
                    binding.provider_generation,
                    binding.terminal_id,
                    binding.terminal_incarnation,
                    screen_max_bytes=screen_max_bytes,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._unknown(participant_id, binding, "provider-inspect-timeout")
            return
        except Exception as exc:
            self._unknown(
                participant_id,
                binding,
                f"provider-inspect-failed:{type(exc).__name__}",
            )
            return
        if epoch != self._epochs.get(binding.provider_id, 0):
            self._unknown(participant_id, binding, "provider-presence-changed-during-inspect")
            return
        await self._accept(participant_id, binding, result)

    async def _accept(
        self,
        participant_id: str,
        expected: TerminalBindingRecord,
        result: Mapping[str, object],
    ) -> None:
        terminal = result.get("terminal")
        presence = result.get("presence")
        report_revision = result.get("report_revision")
        if not isinstance(terminal, Mapping) or not isinstance(presence, Mapping):
            self._unknown(participant_id, expected, "provider-inspect-invalid")
            return
        try:
            current = self.binding(participant_id)
        except Exception:
            self._unknown(participant_id, expected, "provider-binding-unavailable")
            return
        service = self._terminal_service
        if (
            current is None
            or service is None
            or not self._same_route(expected, current)
            or not service.connections.is_current(current.provider_id, current.provider_generation)
            or service.connections.health(current.provider_id) != "online"
            or current.health != "healthy"
            or not self._terminal_matches(current, terminal)
            or result.get("provider_generation") != current.provider_generation
            or type(report_revision) is not int
            or report_revision != current.report_revision
        ):
            self._unknown(participant_id, expected, "provider-evidence-mismatch")
            return
        state_value = presence.get("state")
        revision = presence.get("revision")
        if state_value not in {state.value for state in PresenceState} or type(revision) is not int:
            self._unknown(participant_id, current, "provider-presence-invalid")
            return
        prior = self._observations.get(participant_id)
        if (
            prior is not None
            and prior.binding_key == self._binding_key(current)
            and (
                revision < prior.presence_revision
                or (revision == prior.presence_revision and state_value != prior.state.value)
            )
        ):
            self._unknown(participant_id, current, "provider-presence-regressed")
            return
        reason_value = presence.get("reason")
        reason = (
            str(reason_value)
            if isinstance(reason_value, str) and reason_value
            else f"provider-{state_value}"
        )
        observed_mono = self._clock()
        observed_at = self._wall_clock()
        screen = result.get("screen")
        screen_value = screen if isinstance(screen, str) else None
        lifecycle = result.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("alive") is False:
            process = terminal.get("process")
            evidence = ProviderExitEvidence(
                participant_id=participant_id,
                provider_id=current.provider_id,
                provider_generation=current.provider_generation,
                terminal_id=current.terminal_id,
                terminal_incarnation=current.terminal_incarnation,
                occupant_evidence=dict(current.occupant_evidence),
                process_facts=(None if process is None else dict(process)),
                report_revision=report_revision,
                presence_revision=revision,
                lifecycle=dict(lifecycle),
            )
            if self._exit_handler is None or not await self._handle_exit(evidence):
                self._observations[participant_id] = _Observation(
                    self._binding_key(current),
                    PresenceState.UNKNOWN,
                    "terminal-exit-unsettled",
                    revision,
                    observed_at,
                    observed_mono,
                    screen_value,
                )
                return
            state_value = PresenceState.ABSENT.value
            reason = "terminal-exited"
        self._observations[participant_id] = _Observation(
            self._binding_key(current),
            PresenceState(state_value),
            reason,
            revision,
            observed_at,
            observed_mono,
            screen_value,
        )

    async def _handle_exit(self, evidence: ProviderExitEvidence) -> bool:
        assert self._exit_handler is not None
        try:
            return await self._exit_handler(evidence)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "authoritative provider exit settlement failed for %s",
                evidence.participant_id,
            )
            return False

    def _unknown(self, participant_id: str, binding: TerminalBindingRecord, reason: str) -> None:
        prior = self._observations.get(participant_id)
        presence_revision = (
            prior.presence_revision
            if prior is not None and prior.binding_key == self._binding_key(binding)
            else 0
        )
        self._observations[participant_id] = _Observation(
            self._binding_key(binding),
            PresenceState.UNKNOWN,
            reason,
            presence_revision,
            self._wall_clock(),
            self._clock(),
            None,
        )

    @staticmethod
    def _binding_key(binding: TerminalBindingRecord) -> tuple[object, ...]:
        return (
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
            dict(binding.occupant_evidence),
            None if binding.process_facts is None else dict(binding.process_facts),
        )

    @staticmethod
    def _same_route(left: TerminalBindingRecord, right: TerminalBindingRecord) -> bool:
        return ProviderPresenceSource._binding_key(left) == ProviderPresenceSource._binding_key(
            right
        )

    @staticmethod
    def _terminal_matches(binding: TerminalBindingRecord, terminal: Mapping[str, object]) -> bool:
        process = terminal.get("process")
        return (
            terminal.get("provider_id") == binding.provider_id
            and terminal.get("provider_generation") == binding.provider_generation
            and terminal.get("terminal_id") == binding.terminal_id
            and terminal.get("terminal_incarnation") == binding.terminal_incarnation
            and terminal.get("occupant") == binding.occupant_evidence
            and (process is None or isinstance(process, Mapping))
            and (binding.process_facts is None or process == binding.process_facts)
        )


__all__ = ["ExitHandler", "ProviderExitEvidence", "ProviderPresenceSource"]
