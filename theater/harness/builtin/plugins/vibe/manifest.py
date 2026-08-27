"""Manifest for the shipped Vibe harness."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from theater.harness.base import APPROVALS
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    IdentityManifest,
    LaunchManifest,
    LineageManifest,
    ModelDiscoveryManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.transcript import file_stream_floor

from .launch import discover_models, plan_launch, resume_launch_overlay
from .observer import (
    VibeObserver,
    admit_operator_candidate,
    classify_screen,
    native_children,
    source_factory,
    transcript_candidates,
)

_TRANSCRIPT_CHANNEL = ChannelDeclaration(
    id="transcript",
    kind=ChannelKind.TRANSCRIPT,
    capabilities=tuple(
        ChannelCapability(signal, SignalOwnership.PRIMARY)
        for signal in (
            SignalKind.IDENTITY,
            SignalKind.CONTENT,
            SignalKind.TURN,
            SignalKind.MODEL,
            SignalKind.TOOL,
            SignalKind.TIMING,
            SignalKind.USAGE,
            SignalKind.LINEAGE,
        )
    ),
)


def manifest_for_roots(
    root: Path | None = None,
    correlation_root: Path | None = None,
    *,
    isolated: bool = False,
) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="vibe",
        icon="▤",
        aliases=("Vibe", "mistral-vibe", "mistral_vibe"),
        launch=LaunchManifest(
            planner=partial(plan_launch, correlation_root=correlation_root),
            approvals=APPROVALS,
            supports_model=True,
            supports_reasoning_effort=False,
            supports_resume=True,
            resume_planner=resume_launch_overlay,
            resume_takes_prompt=True,
            resume_strategy="continue",
        ),
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=partial(
                    source_factory,
                    root=root,
                    correlation_root=correlation_root,
                    isolated=isolated,
                ),
                channel=_TRANSCRIPT_CHANNEL,
            ),
            screen=ScreenManifest(classifier=classify_screen),
            identity=IdentityManifest(
                stream_floor=file_stream_floor,
                transcript_candidates=partial(
                    transcript_candidates,
                    root=root,
                    correlation_root=correlation_root,
                ),
                operator_candidate_admitter=partial(
                    admit_operator_candidate,
                    root=root,
                    correlation_root=correlation_root,
                ),
            ),
            lineage=LineageManifest(native_children=native_children),
            trajectory_capabilities=VibeObserver.trajectory_capabilities,
        ),
        models=ModelDiscoveryManifest(discoverer=discover_models),
    )


MANIFEST = manifest_for_roots()
