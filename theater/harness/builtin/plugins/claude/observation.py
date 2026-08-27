"""Claude durable-source and screen declarations."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path

from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.manifest import (
    IdentityManifest,
    LineageManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.transcript import file_stream_floor

from .callbacks import (
    native_children,
    operator_candidate_admitter,
    receipt_validator,
    screen_classifier,
    source_factory,
    transcript_candidates,
)
from .observer import ClaudeCodeObserver

TRANSCRIPT = ChannelDeclaration(
    id="transcript",
    kind=ChannelKind.TRANSCRIPT,
    capabilities=(
        ChannelCapability(SignalKind.IDENTITY, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.MODEL, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TOOL, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TIMING, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.USAGE, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.LINEAGE, SignalOwnership.PRIMARY),
    ),
)

OBSERVATION = ObservationManifest(
    primary=SourceManifest(factory=source_factory, channel=TRANSCRIPT),
    screen=ScreenManifest(classifier=screen_classifier),
    identity=IdentityManifest(
        stream_floor=file_stream_floor,
        transcript_candidates=transcript_candidates,
        receipt_validator=receipt_validator,
        operator_candidate_admitter=operator_candidate_admitter,
    ),
    lineage=LineageManifest(native_children=native_children),
    trajectory_capabilities=ClaudeCodeObserver.trajectory_capabilities,
)


def observation_for(root: Path) -> ObservationManifest:
    """Build the same observation manifest against an explicit transcript root."""
    primary = OBSERVATION.primary
    if primary is None:
        raise RuntimeError("Claude manifest has no primary source")
    return replace(
        OBSERVATION,
        primary=replace(primary, factory=partial(source_factory, root=root)),
        identity=IdentityManifest(
            stream_floor=file_stream_floor,
            transcript_candidates=partial(transcript_candidates, root=root),
            receipt_validator=partial(receipt_validator, root=root),
            operator_candidate_admitter=partial(operator_candidate_admitter, root=root),
        ),
    )
