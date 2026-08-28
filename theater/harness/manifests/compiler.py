"""Compile a validated manifest into existing harness runtime contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    NativeChildrenContext,
    OperatorCandidateContext,
    ReceiptValidationContext,
    ResumeContext,
    ResumePreflightContext,
    ScreenContext,
    StreamFloorContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.channels import ChannelDeclaration
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.harness import Harness, LaunchParameterSupport
from theater.harness.contracts.launch import LaunchPlan, NativeChild, ResumeLaunchOverlay
from theater.harness.contracts.manifest import (
    EnrichmentManifest,
    HarnessManifest,
    ObservationManifest,
)
from theater.harness.contracts.observation import HarnessObserver, ScreenKind, ScreenReading
from theater.harness.contracts.source import Source, StreamPoint, TranscriptCandidate
from theater.harness.manifests.validation import validate_manifest
from theater.models import BadRequest, Participant
from theater.provenance import TranscriptProvenance

_UNBOUND_PARTICIPANT_ID = "unbound"


class ManifestHarnessObserver(HarnessObserver):
    """A generic observer that forwards only typed manifest callbacks."""

    def __init__(
        self,
        observation: ObservationManifest,
        *,
        harness_name: str | None = None,
    ) -> None:
        self._primary = observation.primary
        self._screen = observation.screen
        self._identity = observation.identity
        self._lineage = observation.lineage
        self._enrichments = observation.enrichments
        self._harness_name = harness_name
        self.has_transcript = self._primary is not None
        self.trajectory_capabilities = observation.trajectory_capabilities

    def enrichment_manifests(self) -> tuple[EnrichmentManifest, ...]:
        """Expose validated enrichment declarations without plugin inspection."""
        return self._enrichments

    def primary_channel_declaration(self) -> ChannelDeclaration | None:
        """Expose the validated primary declaration without plugin inspection."""
        return None if self._primary is None else self._primary.channel

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Source:
        return self._open(
            ParticipantObservationContext(
                participant_id=_UNBOUND_PARTICIPANT_ID,
                cwd=cwd,
                session_id=session_id,
                after=after,
                participant_scoped=False,
            )
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
        return self._open(
            ParticipantObservationContext(
                participant_id=participant_id,
                cwd=cwd,
                session_id=session_id,
                after=after,
                session_provenance=session_provenance,
                known_location=known_location,
            )
        )

    def open_source_context(self, context: ParticipantObservationContext) -> Source:
        return self._open(context)

    def _open(self, context: ParticipantObservationContext) -> Source:
        if self._primary is None:
            return super().open_source(
                cwd=context.cwd,
                session_id=context.session_id,
                after=context.after,
            )
        source = self._primary.factory(context)
        if not isinstance(source, Source):
            raise TypeError(
                "manifest source factory must return a theater.harness.contracts.source.Source"
            )
        return source

    def is_idle_screen(self, capture: str) -> bool:
        return self.screen_reading(capture).kind is ScreenKind.PROMPT

    def screen_reading(self, capture: str) -> ScreenReading:
        reading = self._screen.classifier(ScreenContext(capture=capture))
        if not isinstance(reading, ScreenReading):
            raise TypeError(
                "manifest screen classifier must return a "
                "theater.harness.contracts.observation.ScreenReading"
            )
        return reading

    def native_children(self, transcript: Path) -> list[NativeChild]:
        callback = self._lineage.native_children
        if callback is None:
            return super().native_children(transcript)
        children = callback(NativeChildrenContext(transcript=transcript))
        return _checked_sequence(children, NativeChild, "native children")

    def stream_floor(self, location: str) -> StreamPoint | None:
        callback = self._identity.stream_floor
        if callback is None:
            return super().stream_floor(location)
        point = callback(StreamFloorContext(location=location))
        if point is not None and not isinstance(point, StreamPoint):
            raise TypeError("manifest stream-floor reader must return StreamPoint or None")
        return point

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        callback = self._identity.transcript_candidates
        if callback is None:
            return super().transcript_candidates(cwd=cwd, domain=domain, after=after)
        candidates = callback(TranscriptCandidatesContext(cwd=cwd, domain=domain, after=after))
        return _checked_sequence(candidates, TranscriptCandidate, "transcript candidates")

    def validate_transcript_receipt(
        self,
        *,
        payload: Mapping[str, object],
        cwd: str | None,
        expected_session_id: str | None,
    ) -> TranscriptCandidate:
        callback = self._identity.receipt_validator
        if callback is None:
            if self._harness_name is not None:
                raise ValueError(
                    f"harness {self._harness_name!r} does not implement "
                    "validate_transcript_receipt; a plugin must implement this hook "
                    "to use transcript receipts. See docs/harness-plugins.md"
                )
            return super().validate_transcript_receipt(
                payload=payload,
                cwd=cwd,
                expected_session_id=expected_session_id,
            )
        candidate = callback(
            ReceiptValidationContext(
                payload=payload,
                cwd=cwd,
                expected_session_id=expected_session_id,
            )
        )
        return _checked_value(candidate, TranscriptCandidate, "receipt validator")

    @property
    def supports_transcript_receipts(self) -> bool:
        return self._identity.receipt_validator is not None

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        callback = self._identity.operator_candidate_admitter
        if callback is None:
            return super().admit_operator_candidate(
                cwd=cwd,
                candidate=candidate,
                domain=domain,
                after=after,
            )
        admitted = callback(
            OperatorCandidateContext(
                cwd=cwd,
                candidate=candidate,
                domain=domain,
                after=after,
            )
        )
        return _checked_value(admitted, TranscriptCandidate, "operator candidate admitter")


class _CompiledHarness(Harness):
    """A generic runtime harness backed by one validated manifest."""

    launch_parameter_support: LaunchParameterSupport

    def __init__(self, name: str, manifest: HarnessManifest) -> None:
        self.name = name
        self.binary = manifest.binary
        self.binaries = manifest.binaries
        self.icon = manifest.icon
        self.aliases = manifest.aliases
        self.observer = ManifestHarnessObserver(manifest.observation, harness_name=name)
        self._launch = manifest.launch
        self._models = manifest.models
        self.resume_takes_prompt = manifest.launch.resume_takes_prompt
        self.resume_strategy = manifest.launch.resume_strategy
        self.launch_parameter_support = LaunchParameterSupport(
            model=manifest.launch.supports_model,
            reasoning_effort=manifest.launch.supports_reasoning_effort,
            resume=manifest.launch.supports_resume,
        )

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in self._launch.approvals:
            choices = ", ".join(self._launch.approvals)
            raise BadRequest(f"approval must be one of {choices}, got {approval!r}")
        support = self.launch_parameter_support
        if model is not None and not support.model:
            raise BadRequest(f"harness {self.name!r} does not support model selection")
        if reasoning_effort is not None and not support.reasoning_effort:
            raise BadRequest(f"harness {self.name!r} does not support reasoning effort selection")
        if resume is not None and not support.resume:
            raise BadRequest(f"harness {self.name!r} does not support resume")
        plan = self._launch.planner(
            LaunchContext(
                participant_id=participant_id,
                prompt=prompt,
                config_path=config_path,
                approval=approval,
                model=model,
                reasoning_effort=reasoning_effort,
                resume=resume,
            )
        )
        if not isinstance(plan, LaunchPlan):
            raise TypeError("manifest launch planner must return a LaunchPlan")
        return plan

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        if self._launch.resume_planner is None:
            return super().resume_launch_overlay(
                predecessor=predecessor,
                trusted_session_owners=trusted_session_owners,
            )
        overlay = self._launch.resume_planner(
            ResumeContext(
                predecessor=predecessor,
                trusted_session_owners=tuple(trusted_session_owners),
            )
        )
        if not isinstance(overlay, ResumeLaunchOverlay):
            raise TypeError("manifest resume planner must return a ResumeLaunchOverlay")
        return overlay

    def resume_preflight(self, *, predecessor: Participant) -> None:
        callback = self._launch.resume_preflight
        if callback is None:
            return
        result = callback(ResumePreflightContext(predecessor=predecessor))
        if result is not None:
            raise TypeError("manifest resume preflight must return None")

    def discover_models(self) -> list[str]:
        if self._models is None:
            return super().discover_models()
        discovered = self._models.discoverer(
            ModelDiscoveryContext(name=self.name, binary=self.binary)
        )
        if isinstance(discovered, (str, bytes)) or not isinstance(discovered, Sequence):
            raise TypeError("manifest model discoverer must return a sequence of non-blank strings")
        models = list(discovered)
        if any(not isinstance(model, str) or not model.strip() for model in models):
            raise TypeError("manifest model discoverer must return only non-blank strings")
        return models


def compile_manifest(name: str, manifest: HarnessManifest) -> Harness:
    """Validate and compile immutable data into existing runtime contracts."""
    validate_manifest(name, manifest)
    return _CompiledHarness(name, manifest)


def _checked_sequence[T](value: object, item_type: type[T], label: str) -> list[T]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"manifest {label} callback must return a sequence")
    items = list(value)
    if any(not isinstance(item, item_type) for item in items):
        raise TypeError(f"manifest {label} callback returned an invalid item")
    return items


def _checked_value[T](value: object, value_type: type[T], label: str) -> T:
    if not isinstance(value, value_type):
        raise TypeError(f"manifest {label} must return {value_type.__name__}")
    return value


__all__ = ["ManifestHarnessObserver", "compile_manifest"]
