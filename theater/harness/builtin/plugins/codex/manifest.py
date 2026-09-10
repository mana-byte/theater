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
    ControlManifest,
    HarnessManifest,
    HookChannelManifest,
    IdentityManifest,
    InterruptPlan,
    LaunchManifest,
    McpRenderingManifest,
    ObservationManifest,
    OtelChannelManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeManifest,
)
from theater.harness.transcript import file_stream_floor

from .constants import CODEX_BINARY
from .identity import admit_operator_candidate, transcript_candidates
from .launch import plan_launch, resume_launch_overlay
from .mcp import render_mcp_servers
from .observer import CodexObserver
from .runtime import codex_runtime_factory
from .runtime_plan import plan_codex_runtime_backend, probe_codex_compatibility
from .screen import screen_reading
from .source import source_for

_NATIVE_HOOKS = HookChannelManifest(
    declaration=ChannelDeclaration(id="native-hooks", kind=ChannelKind.HOOK),
    unavailable_reason=(
        "Codex hooks need verified launch-local configuration, trust behavior, and installed "
        "payloads before Theater can enable them"
    ),
)

_NATIVE_OTEL = OtelChannelManifest(
    declaration=ChannelDeclaration(id="native-otel", kind=ChannelKind.OTEL),
    unavailable_reason="endpoint choice replaces exporter; no safe fan-out.",
)

#: The runtime's single live channel: the CodexRuntime's live Source on the
#: private app-server backend. It is a live channel, never a transcript or a
#: database surrogate, and a future HybridSource composes it with the durable
#: rollout reader above.
_NATIVE_LIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(
        id="native-live",
        kind=ChannelKind.LIVE,
        capabilities=(
            ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
            ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
        ),
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
        enrichments=(_NATIVE_HOOKS, _NATIVE_OTEL),
    ),
    controls=ControlManifest(interrupt=InterruptPlan(keys=("Escape",))),
    mcp=McpRenderingManifest(renderer=render_mcp_servers),
    runtime=RuntimeManifest(
        probe=probe_codex_compatibility,
        plan=plan_codex_runtime_backend,
        factory=codex_runtime_factory,
        channel=_NATIVE_LIVE,
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
