"""Contracts for harness observation and screen classification."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.launch import NativeChild
from theater.harness.contracts.source import StreamPoint, TranscriptCandidate
from theater.provenance import TranscriptProvenance
from theater.trajectory import TrajectoryCapabilities

if TYPE_CHECKING:
    from theater.harness.contracts.channels import ChannelDeclaration
    from theater.harness.contracts.manifest import EnrichmentManifest
    from theater.harness.contracts.source import Source


class ScreenKind(StrEnum):
    """What the rendered screen is showing, at the level a consumer needs.

    At ``approval``/``trust`` Enter is a button press, so injecting a prompt can auto-approve.
    """

    WORKING = "working"
    PROMPT = "prompt"
    APPROVAL = "approval"
    TRUST = "trust"
    UNKNOWN = "unknown"


class ScreenConfidence(StrEnum):
    """How sure the observer is about its classification.

    ``low`` is the honest default for heuristics over a text scrape, and the shim's only answer.
    """

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ScreenReading:
    """A structured classification of the rendered screen.

    Rescue must never falsely say "prompt"; the send gate must never falsely say "blocked". So each
    consumer resolves ``unknown`` itself — one default would recreate the boolean's ambiguity.
    """

    kind: ScreenKind
    confidence: ScreenConfidence = ScreenConfidence.LOW


class HarnessObserver(ABC):
    """How to watch one harness. One instance per harness, held by it.

    Shared by every session: per-session state belongs on the `Source`; locating config lives here.
    """

    #: True selects the transcript watch loop; False falls back to capture-pane.
    has_transcript: bool = True
    trajectory_capabilities: TrajectoryCapabilities = TrajectoryCapabilities()

    def enrichment_manifests(self) -> tuple[EnrichmentManifest, ...]:
        """Return immutable declared enrichment manifests."""
        return ()

    def primary_channel_declaration(self) -> ChannelDeclaration | None:
        """Return the declared durable channel when one exists."""
        return None

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        """A live view of one participant's output, for the reducer to poll.

        Raises rather than being abstract: only observers with `has_transcript = True` must
        implement it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} sets has_transcript = "
            f"{self.has_transcript!r} but does not implement open_source"
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        """Open a source with the Theater participant identity available.

        Defaults to :meth:`open_source`; overrides may accept more (e.g. ``pane_pid``), never less.
        """
        return self.open_source(cwd=cwd, session_id=session_id, after=after)

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        """Open a source from the complete participant context."""
        return self.open_source_for(
            participant_id=context.participant_id,
            cwd=context.cwd,
            session_id=context.session_id,
            after=context.after,
            session_provenance=context.session_provenance,
            known_location=context.known_location,
        )

    @abstractmethod
    def is_idle_screen(self, capture: str) -> bool:
        """Does the rendered screen show a bare prompt (waiting for input)?

        Never a false positive: it hides activity and can finish a job with a partial answer.
        It guards rescue; approval modals need ``screen_reading``.
        """

    def screen_reading(self, capture: str) -> ScreenReading:
        """A structured classification of the rendered screen.

        Not abstract: the shim maps ``is_idle_screen`` to prompt/unknown at ``low`` confidence so
        boolean-only plugins work; not-idle is ``unknown``, not ``working``, so gates do not send.
        """
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Sub-agents this session spawned by itself, outside Theater (spec §5); none by default."""
        return []

    def stream_floor(self, location: str) -> StreamPoint | None:
        """Capture the current stream position of a transcript, or None.

        The floor stops a successor claiming a predecessor's stale records; ``None`` is encoded as
        ``UNKNOWN_FLOOR`` so completion is suppressed rather than treated as a cold spawn.
        """
        return None

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        """Operator recovery candidates, explicitly not participant-attributed content."""
        return []

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        """Validate an opaque lifecycle-hook receipt into a transcript candidate.

        Core never inspects the payload; reject with ``ValueError`` (mapped to ``BadRequest``).
        Not abstract so plugins without receipts keep working.
        """
        raise ValueError(
            f"harness {type(self).__name__} does not implement "
            "validate_transcript_receipt; a plugin must implement this hook "
            "to use transcript receipts. See docs/harness-plugins.md"
        )

    @property
    def supports_transcript_receipts(self) -> bool:
        """Whether this observer accepts the generic transcript receipt RPC."""
        return (
            type(self).validate_transcript_receipt
            is not HarnessObserver.validate_transcript_receipt
        )

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        """Validate an operator-named candidate before the daemon persists trust."""
        raise ValueError(f"{type(self).__name__} has no operator-bindable transcript")
