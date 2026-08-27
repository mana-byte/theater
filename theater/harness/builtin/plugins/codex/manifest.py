"""Codex package manifest."""

from dataclasses import replace
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
    HookChannelManifest,
    IdentityManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.transcript import file_stream_floor

from .constants import CODEX_BINARY
from .identity import admit_operator_candidate, transcript_candidates
from .launch import plan_launch, resume_launch_overlay
from .observer import CodexObserver
from .screen import screen_reading
from .source import source_for

_NATIVE_HOOKS = HookChannelManifest(
    declaration=ChannelDeclaration(id="native-hooks", kind=ChannelKind.HOOK),
    unavailable_reason=(
        "Codex hooks need verified launch-local configuration, trust behavior, and installed "
        "payloads before Theater can enable them"
    ),
)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary=CODEX_BINARY,
    icon="◉",
    aliases=("codex-cli", "codex_cli", "openai-codex", "Codex"),
    launch=LaunchManifest(
        planner=plan_launch,
        approvals=APPROVALS,
        supports_model=True,
        supports_reasoning_effort=True,
        supports_resume=True,
        resume_planner=resume_launch_overlay,
        resume_strategy="fork",
    ),
    observation=ObservationManifest(
        primary=SourceManifest(
            factory=source_for,
            channel=ChannelDeclaration(
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
                ),
            ),
        ),
        screen=ScreenManifest(classifier=screen_reading),
        identity=IdentityManifest(
            stream_floor=file_stream_floor,
            transcript_candidates=transcript_candidates,
            operator_candidate_admitter=admit_operator_candidate,
        ),
        trajectory_capabilities=CodexObserver.trajectory_capabilities,
        enrichments=(_NATIVE_HOOKS,),
    ),
)


def manifest_for_root(root: Path) -> HarnessManifest:
    """Bind test-only transcript-root configuration into Codex callbacks."""
    primary = MANIFEST.observation.primary
    if primary is None:
        raise RuntimeError("Codex manifest has no primary source")
    return replace(
        MANIFEST,
        launch=replace(
            MANIFEST.launch,
            resume_planner=partial(resume_launch_overlay, root=root),
        ),
        observation=replace(
            MANIFEST.observation,
            primary=replace(primary, factory=partial(source_for, root=root)),
            identity=replace(
                MANIFEST.observation.identity,
                transcript_candidates=partial(transcript_candidates, root=root),
                operator_candidate_admitter=partial(admit_operator_candidate, root=root),
            ),
        ),
    )
