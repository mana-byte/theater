"""OpenCode's immutable package manifest."""

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
    ModelDiscoveryManifest,
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

from .frontend import install_opencode_tui_extension
from .launch import discover_models, plan_launch, resume_launch_overlay
from .observer import (
    OpenCodeObserver,
    admit_operator_candidate_context,
    classify_screen,
    read_transcript_candidates,
    source_factory,
    validate_receipt,
)
from .render_mcp import render_mcp_servers
from .runtime import opencode_frontend_runtime_factory
from .runtime_plan import probe_opencode_compatibility

_NATIVE_HOOKS = HookChannelManifest(
    declaration=ChannelDeclaration(id="native-hooks", kind=ChannelKind.HOOK),
    unavailable_reason=(
        "OpenCode's launch-local metadata hook does not provide the authenticated, bounded "
        "trajectory-fact transport required by this channel"
    ),
)

_NATIVE_OTEL = OtelChannelManifest(
    declaration=ChannelDeclaration(id="native-otel", kind=ChannelKind.OTEL),
    unavailable_reason="one endpoint, no safe fan-out or stable join.",
)

_DATABASE_CHANNEL = ChannelDeclaration(
    id="opencode-database",
    kind=ChannelKind.DATABASE,
    capabilities=(
        ChannelCapability(SignalKind.IDENTITY, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.MODEL, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TOOL, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.TIMING, SignalOwnership.PRIMARY),
        ChannelCapability(SignalKind.USAGE, SignalOwnership.PRIMARY),
    ),
)

_OPENCODE_TUI_LIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(
        id="opencode-tui-live",
        kind=ChannelKind.LIVE,
        capabilities=(ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT),),
    ),
    drives_job_completion=False,
)

MANIFEST = HarnessManifest(
    api_version=MANIFEST_API_VERSION,
    binary="opencode",
    icon="◇",
    aliases=("open-code", "open_code", "OpenCode", "opencode-ai"),
    launch=LaunchManifest(
        planner=plan_launch,
        approvals=APPROVALS,
        supports_model=True,
        supports_reasoning_effort=False,
        supports_resume=True,
        resume_planner=resume_launch_overlay,
        resume_takes_prompt=False,
        resume_strategy="fork",
    ),
    observation=ObservationManifest(
        primary=SourceManifest(factory=source_factory, channel=_DATABASE_CHANNEL),
        screen=ScreenManifest(classifier=classify_screen),
        identity=IdentityManifest(
            transcript_candidates=read_transcript_candidates,
            receipt_validator=validate_receipt,
            operator_candidate_admitter=admit_operator_candidate_context,
        ),
        trajectory_capabilities=OpenCodeObserver.trajectory_capabilities,
        enrichments=(_NATIVE_HOOKS, _NATIVE_OTEL),
    ),
    controls=ControlManifest(
        interrupt=InterruptPlan(keys=("Escape", "Escape"), inter_key_delay_seconds=0.05)
    ),
    models=ModelDiscoveryManifest(discoverer=discover_models),
    mcp=McpRenderingManifest(renderer=render_mcp_servers),
    runtime=RuntimeManifest(
        probe=probe_opencode_compatibility,
        plan=None,
        factory=opencode_frontend_runtime_factory,
        channel=_OPENCODE_TUI_LIVE,
        host=RuntimeHost.FRONTEND,
        frontend_installer=install_opencode_tui_extension,
        legacy_fallback=frozenset(
            {
                RuntimeCapability.SEND,
                RuntimeCapability.QUEUE_FOLLOWUP,
                RuntimeCapability.INTERRUPT,
            }
        ),
        unavailable_capabilities=frozenset(
            {
                RuntimeCapability.STEER,
                RuntimeCapability.SETTINGS_UPDATE,
            }
        ),
    ),
)


def manifest_for_paths(
    db: Path | None = None, correlation_dir: Path | None = None
) -> HarnessManifest:
    if db is None and correlation_dir is None:
        return MANIFEST
    observation = MANIFEST.observation
    primary = observation.primary
    if primary is None:
        raise RuntimeError("OpenCode manifest has no primary source")
    return replace(
        MANIFEST,
        launch=replace(
            MANIFEST.launch,
            planner=partial(plan_launch, db=db),
            resume_planner=partial(resume_launch_overlay, db=db),
        ),
        observation=replace(
            observation,
            primary=replace(
                primary,
                factory=partial(source_factory, db=db, correlation_dir=correlation_dir),
            ),
            identity=replace(
                observation.identity,
                transcript_candidates=partial(read_transcript_candidates, db=db),
                receipt_validator=partial(validate_receipt, db=db),
                operator_candidate_admitter=partial(admit_operator_candidate_context, db=db),
            ),
        ),
    )
