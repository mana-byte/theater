"""Stub sources and a baseline manifest for the manifest tests."""

from __future__ import annotations

from theater.harness.contracts.callbacks import LaunchContext, ScreenContext
from theater.harness.contracts.channels import ChannelCapability, ChannelDeclaration, ChannelKind
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    ControlManifest,
    HarnessManifest,
    LaunchManifest,
    ModelDiscoveryManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading
from theater.harness.contracts.source import Batch, Source


class StubSource(Source):
    async def read(self) -> Batch:
        return Batch()


def plan(context: LaunchContext) -> LaunchPlan:
    return LaunchPlan(argv=["acme", context.participant_id])


def source(_context: ParticipantObservationContext) -> Source:
    return StubSource()


def screen(_context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)


def primary_channel(
    capabilities: tuple[ChannelCapability, ...] = (),
    *,
    channel_id: str = "primary",
) -> ChannelDeclaration:
    return ChannelDeclaration(
        id=channel_id,
        kind=ChannelKind.TRANSCRIPT,
        capabilities=capabilities,
    )


def manifest(
    *,
    launch: LaunchManifest | None = None,
    observation: ObservationManifest | None = None,
    models: ModelDiscoveryManifest | None = None,
    controls: ControlManifest | None = None,
    aliases: tuple[str, ...] = (),
    binaries: frozenset[str] = frozenset(),
) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="@",
        binaries=binaries,
        aliases=aliases,
        launch=launch
        or LaunchManifest(
            planner=plan,
            approvals=("manual", "edits", "yolo"),
        ),
        observation=observation
        or ObservationManifest(
            primary=SourceManifest(factory=source, channel=primary_channel()),
            screen=ScreenManifest(classifier=screen),
        ),
        controls=controls or ControlManifest(),
        models=models,
    )
