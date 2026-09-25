"""Live-channel registration: the seam between lifecycle and observation.

Neither imports the other; terminal evidence is persisted by the sink before the job finish
is visible. The hub never touches store, registry, or plugins; polling stays the fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from theater.constants.harness import HARNESS_RUNTIME_ID_MAX_CHARS
from theater.harness.channels.wakeup import WakeupHub, WakeupSignal
from theater.harness.contracts.runtime import LiveChannelDeclaration
from theater.harness.contracts.source import Source
from theater.models import Job

__all__ = [
    "ActiveJobForTurn",
    "EvidenceSink",
    "LiveObservationHub",
    "LiveRegistration",
    "LiveRegistrationError",
]

logger = logging.getLogger("theater.observer.live")


def _bounded_id(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise LiveRegistrationError(f"live registration {label} must be a bounded non-blank string")


def _bounded_native_id(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > HARNESS_RUNTIME_ID_MAX_CHARS:
        raise LiveRegistrationError(f"live registration {label} must be a bounded non-blank string")


#: The exact completion path: persist the outcome, then finish its mapped job.
#: The signature is ``ControlService.record_terminal_evidence``.
EvidenceSink = Callable[..., Awaitable[Job | None]]

#: The exact active-job lookup for one native turn: fails closed to ``None``.
#: The signature is ``ControlService.active_job_for_native_turn``.
ActiveJobForTurn = Callable[..., Job | None]


class LiveRegistrationError(ValueError):
    """An invalid live-channel registration."""


@dataclass(frozen=True, slots=True)
class LiveRegistration:
    """One participant's effective live wiring facts.

    The callables are injected so observation never imports the control service.
    """

    participant_id: str
    live_source: Source
    channel: LiveChannelDeclaration
    backend_generation: int
    native_session_id: str | None = None
    evidence_sink: EvidenceSink | None = None
    active_job_for_turn: ActiveJobForTurn | None = None

    def __post_init__(self) -> None:
        _bounded_id(self.participant_id, "participant_id")
        if not isinstance(self.live_source, Source):
            raise LiveRegistrationError("live_source must implement Source")
        if not isinstance(self.channel, LiveChannelDeclaration):
            raise LiveRegistrationError("channel must be a LiveChannelDeclaration")
        if type(self.backend_generation) is not int or self.backend_generation < 0:
            raise LiveRegistrationError("backend_generation must be a non-negative integer")
        if self.native_session_id is not None:
            _bounded_native_id(self.native_session_id, "native_session_id")
        for name in ("evidence_sink", "active_job_for_turn"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise LiveRegistrationError(f"{name} must be callable or null")


class LiveObservationHub:
    """Per-participant live registrations plus their wake signals.

    Bounded by live participants: wake signals are dropped with their registrations.
    """

    def __init__(self, on_change: Callable[[str], None] | None = None) -> None:
        self._registrations: dict[str, LiveRegistration] = {}
        self._wakeups = WakeupHub()
        self._on_change = on_change
        # participant id -> live source currently holding the activity callback.
        self._activity_sources: dict[str, Source] = {}

    # ---- registration ------------------------------------------------------

    def register(self, registration: LiveRegistration) -> None:
        """Install one participant's live wiring, replacing any previous one.

        The wake signal is cleared so stale wakes cannot spin the recomposed watcher, then set
        once to pick up buffered data; the activity callback moves to the new source.
        """
        if not isinstance(registration, LiveRegistration):
            raise LiveRegistrationError("register requires a LiveRegistration")
        pid = registration.participant_id
        replaced = self._registrations.get(pid)
        if replaced is not None and replaced.live_source is not registration.live_source:
            self._detach_activity(replaced.live_source)
        self._registrations[pid] = registration
        self._install_activity(registration)
        if replaced is not None:
            self._wakeups.discard(pid)
        self._wakeups.signal(pid)
        self._wake(pid)
        self._changed(pid)
        logger.info(
            "live wiring registered for %s (backend generation %s, channel %s)",
            pid,
            registration.backend_generation,
            registration.channel.channel.id,
        )

    def unregister(self, participant_id: str) -> None:
        """Return one participant to durable-only observation."""
        registration = self._registrations.pop(participant_id, None)
        if registration is None:
            return
        self._detach_activity(registration.live_source)
        self._activity_sources.pop(participant_id, None)
        self._wakeups.discard(participant_id)
        self._changed(participant_id)
        logger.info("live wiring unregistered for %s", participant_id)

    def registration_for(self, participant_id: str) -> LiveRegistration | None:
        return self._registrations.get(participant_id)

    def participants(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    # ---- wakeups -----------------------------------------------------------

    def wake(self, participant_id: str) -> None:
        """Announce live data for one participant; the prompt, race-safe hook.

        Safe before registration (fires on first registration); polling remains the fallback.
        """
        self._wakeups.wake(participant_id)

    def wake_signal(self, participant_id: str) -> WakeupSignal | None:
        """The participant's wake signal, or None without live wiring."""
        return self._wakeups.existing(participant_id)

    # ---- arrival-driven activity --------------------------------------------

    def _install_activity(self, registration: LiveRegistration) -> None:
        """Install the optional, duck-typed arrival wake callback on the live source.

        It only sets the wake signal, coalescing any number of arrivals into one read.
        """
        attach = getattr(registration.live_source, "set_activity_callback", None)
        pid = registration.participant_id
        if not callable(attach):
            return

        def _on_activity() -> None:
            self.wake(pid)

        try:
            attach(_on_activity)
        except Exception:
            logger.exception("installing the activity callback for %s failed", pid)
            return
        self._activity_sources[pid] = registration.live_source

    def _detach_activity(self, source: Source) -> None:
        detach = getattr(source, "set_activity_callback", None)
        if not callable(detach):
            return
        try:
            detach(None)
        except Exception:
            logger.exception("detaching a live activity callback failed")

    # ---- internals -----------------------------------------------------------

    def _wake(self, participant_id: str) -> None:
        self._wakeups.wake(participant_id)

    def _changed(self, participant_id: str) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(participant_id)
        except Exception:
            logger.exception("live wiring change handling failed for %s", participant_id)
