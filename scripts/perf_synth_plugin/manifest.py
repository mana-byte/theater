"""Measurement-only `synth` harness: a Python writer stands in for an agent CLI."""

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
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)

from .launch import plan_launch
from .observer import classify_screen, source_factory

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="synth",
    icon="~",
    launch=LaunchManifest(
        planner=plan_launch,
        approvals=("manual", "edits", "yolo"),
        supports_model=False,
        supports_reasoning_effort=False,
        supports_resume=False,
    ),
    observation=ObservationManifest(
        primary=SourceManifest(
            factory=source_factory,
            channel=ChannelDeclaration(
                id="transcript",
                kind=ChannelKind.TRANSCRIPT,
                capabilities=(
                    ChannelCapability(SignalKind.IDENTITY, SignalOwnership.PRIMARY),
                    ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
                ),
            ),
        ),
        screen=ScreenManifest(classifier=classify_screen),
    ),
)
