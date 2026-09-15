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
    NativeCompatibilityManifest,
    ObservationManifest,
    OtelChannelManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCapability,
    RuntimeCredentialDeclaration,
    RuntimeEndpointDiscovery,
    RuntimeHost,
    RuntimeManifest,
    RuntimeSessionOrder,
)

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
from .runtime_plan import SERVER_STDOUT_MAX_BYTES
from .server_discovery import parse_server_stdout_endpoint
from .server_plan import (
    SERVER_SECRET_ENV,
    plan_opencode_server,
    probe_opencode_server_compatibility,
)
from .server_runtime import opencode_server_runtime_factory

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

#: The detached server topology: SSE state is enrichment; the storage
#: database stays the completion authority, exactly as in the TUI baseline.
_OPENCODE_SERVER_LIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(
        id="opencode-server-live",
        kind=ChannelKind.LIVE,
        capabilities=(ChannelCapability(SignalKind.LIFECYCLE, SignalOwnership.ENRICHMENT),),
    ),
    drives_job_completion=False,
)

#: One core-minted secret authorizes both the serve process and the attach
#: client through OPENCODE_SERVER_PASSWORD; the bytes stay in a 0600 file.
_OPENCODE_SERVER_CREDENTIAL = RuntimeCredentialDeclaration(
    channel_id="opencode-server",
    env=(SERVER_SECRET_ENV,),
)

_OPENCODE_SERVER_DISCOVERY = RuntimeEndpointDiscovery(
    parser=parse_server_stdout_endpoint,
    max_bytes=SERVER_STDOUT_MAX_BYTES,
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
    native_compatibility=NativeCompatibilityManifest(
        qualified_range=">=1.18.29,<1.18.30",
        probe=probe_opencode_compatibility,
    ),
    runtime=RuntimeManifest(
        probe=probe_opencode_server_compatibility,
        plan=plan_opencode_server,
        factory=opencode_server_runtime_factory,
        channel=_OPENCODE_SERVER_LIVE,
        host=RuntimeHost.DETACHED_BACKEND,
        endpoint_discovery=_OPENCODE_SERVER_DISCOVERY,
        runtime_credential=_OPENCODE_SERVER_CREDENTIAL,
        session_order=RuntimeSessionOrder.SESSION_FIRST,
        legacy_fallback=frozenset({RuntimeCapability.INTERRUPT}),
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
