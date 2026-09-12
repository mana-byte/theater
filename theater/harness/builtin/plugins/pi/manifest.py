"""Immutable manifest for the shipped Pi harness."""

from __future__ import annotations

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
    RuntimeCapability,
    RuntimeHost,
    RuntimeManifest,
)
from theater.harness.transcript import file_stream_floor

from .constants import PI_BINARY
from .frontend import install_pi_frontend
from .launch import plan_launch, resume_launch_overlay
from .mcp import render_mcp_servers
from .observer import (
    PiObserver,
    admit_operator_candidate,
    source_factory,
    transcript_candidates,
)
from .runtime import (
    PI_FRONTEND_CHANNEL_ID,
    pi_frontend_runtime_factory,
    probe_pi_frontend_compatibility,
)
from .screen import classify_screen

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
        )
    ),
)

_NATIVE_HOOKS = HookChannelManifest(
    declaration=ChannelDeclaration(id="native-hooks", kind=ChannelKind.HOOK),
    unavailable_reason="Pi extensions have no authenticated Theater hook receipt transport yet.",
)

_NATIVE_OTEL = OtelChannelManifest(
    declaration=ChannelDeclaration(id="native-otel", kind=ChannelKind.OTEL),
    unavailable_reason="Pi has no launch-local OTel exporter configuration.",
)


def manifest_for_root(root: Path | None = None) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary=PI_BINARY,
        icon="π",
        aliases=("pi-agent", "pi_agent", "Pi"),
        launch=LaunchManifest(
            planner=plan_launch,
            # Pi has no Theater-enforced permission prompt or sandbox: it runs
            # with its tools available and Theater injects no approval keystrokes.
            # Advertising manual/edits would promise unenforced safety, so the
            # launch contract admits only the behavior plan_launch produces.
            approvals=("yolo",),
            supports_model=True,
            supports_reasoning_effort=True,
            supports_resume=True,
            resume_planner=resume_launch_overlay,
            resume_takes_prompt=True,
            resume_strategy="continue",
        ),
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=partial(source_factory, root=root),
                channel=_TRANSCRIPT_CHANNEL,
            ),
            screen=ScreenManifest(classifier=classify_screen),
            identity=IdentityManifest(
                stream_floor=file_stream_floor,
                transcript_candidates=partial(transcript_candidates, root=root),
                operator_candidate_admitter=partial(admit_operator_candidate, root=root),
            ),
            trajectory_capabilities=PiObserver.trajectory_capabilities,
            enrichments=(_NATIVE_HOOKS, _NATIVE_OTEL),
        ),
        controls=ControlManifest(interrupt=InterruptPlan(keys=("Escape",))),
        mcp=McpRenderingManifest(renderer=render_mcp_servers),
        runtime=RuntimeManifest(
            probe=probe_pi_frontend_compatibility,
            plan=None,
            factory=pi_frontend_runtime_factory,
            channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(
                    id=PI_FRONTEND_CHANNEL_ID,
                    kind=ChannelKind.LIVE,
                    capabilities=(
                        ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT),
                    ),
                ),
                drives_job_completion=False,
            ),
            host=RuntimeHost.FRONTEND,
            frontend_installer=install_pi_frontend,
            legacy_fallback=frozenset(
                {
                    RuntimeCapability.SEND,
                    RuntimeCapability.QUEUE_FOLLOWUP,
                    RuntimeCapability.INTERRUPT,
                }
            ),
            unavailable_capabilities=frozenset({RuntimeCapability.STEER}),
        ),
    )


MANIFEST = manifest_for_root()
