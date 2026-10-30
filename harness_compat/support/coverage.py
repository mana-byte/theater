"""Which live scenario family proves each declaration a manifest can make, or why none can.

Every declaration found by :func:`harness_compat.support.declarations.declarations` is listed here
exactly once, either under the family whose scenario exercises it or under a reasoned exemption.
There are no patterns: adding a capability to a manifest type fails the completeness test until
someone decides how the live suite will prove it, or writes down why it cannot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NoLive:
    """A declaration with no live obligation, and the reason that stays true."""

    reason: str


Coverage = str | NoLive


def _family(name: str, *declarations: str) -> dict[str, Coverage]:
    return dict.fromkeys(declarations, name)


def _exempt(reason: str, *declarations: str) -> dict[str, Coverage]:
    return dict.fromkeys(declarations, NoLive(reason))


def _merge(*groups: dict[str, Coverage]) -> dict[str, Coverage]:
    """Join the groups, refusing a declaration listed twice."""
    merged: dict[str, Coverage] = {}
    for group in groups:
        for declaration, covered in group.items():
            if declaration in merged:
                raise ValueError(f"{declaration} is listed twice in the coverage registry")
            merged[declaration] = covered
    return merged


COVERAGE: Mapping[str, Coverage] = _merge(
    # --- launch: the real planner, each approval, a ready prompt the plugin's classifier reads
    _family(
        "launch",
        "HarnessManifest.binary",
        "HarnessManifest.launch",
        "LaunchManifest.planner",
        "LaunchManifest.approvals",
        "HarnessManifest.observation",
        "ObservationManifest.screen",
        "ScreenManifest.classifier",
        "CompatManifest.provision",
    ),
    # --- turn: a prompt becomes a done job with the scripted answer and a bound transcript
    _family(
        "turn",
        "ObservationManifest.primary",
        "SourceManifest.factory",
        "SourceManifest.channel",
        "ObservationManifest.identity",
        "IdentityManifest.stream_floor",
        "IdentityManifest.transcript_candidates",
        "IdentityManifest.receipt_validator",
        "ChannelDeclaration.id",
        "ChannelDeclaration.kind",
        "ChannelDeclaration.kind=TRANSCRIPT",
        "ChannelDeclaration.kind=DATABASE",
        "ChannelDeclaration.kind=SCREEN",
        "ChannelDeclaration.capabilities",
        "ChannelCapability.signal",
        "ChannelCapability.signal=IDENTITY",
        "ChannelCapability.signal=LIFECYCLE",
        "ChannelCapability.signal=CONTENT",
        "ChannelCapability.signal=TURN",
        "ChannelCapability.ownership",
        "ChannelCapability.ownership=PRIMARY",
        "RuntimeManifest.legacy_fallback=SEND",
    ),
    # --- interrupt, steer
    _family(
        "interrupt",
        "HarnessManifest.controls",
        "ControlManifest.interrupt",
        "InterruptPlan.keys",
        "InterruptPlan.inter_key_delay_seconds",
        "RuntimeManifest.legacy_fallback=INTERRUPT",
    ),
    _family(
        "steer",
        "ControlManifest.steer",
        "RuntimeManifest.legacy_fallback=STEER",
        "RuntimeManifest.unavailable_capabilities=STEER",
    ),
    # --- resume: history reaches the model once and the daemon records the lineage
    _family(
        "resume",
        "LaunchManifest.supports_resume",
        "LaunchManifest.resume_preflight",
        "LaunchManifest.resume_planner",
        "LaunchManifest.resume_takes_prompt",
        "LaunchManifest.resume_strategy",
        "LaunchManifest.resume_strategy='continue'",
        "LaunchManifest.resume_strategy='fork'",
    ),
    # --- model and effort: a selection reaches the wire
    _family(
        "model",
        "LaunchManifest.supports_model",
        "ChannelCapability.signal=MODEL",
    ),
    _family("effort", "LaunchManifest.supports_reasoning_effort"),
    # --- mcp: the probe tool is gated or runs, a refusal arrives in the declared delivery
    _family(
        "mcp",
        "HarnessManifest.mcp",
        "McpRenderingManifest.renderer",
        "McpRenderingManifest.error_delivery",
        "McpRenderingManifest.error_delivery='is_error'",
        "McpRenderingManifest.error_delivery='result'",
        "CompatManifest.mcp_call",
        "McpCall.name",
        "McpCall.payload",
        "CompatManifest.mcp_discovery",
        "McpDiscovery.name",
        "McpDiscovery.payload",
        "ChannelCapability.signal=TOOL",
    ),
    # --- edit: the CLI's own file-write tool is gated or runs
    _family(
        "edit",
        "CompatManifest.native_edit",
        "NativeEdit.name",
        "NativeEdit.payload",
    ),
    # --- channels: correlated positive facts when available, negative checks when unavailable
    _family(
        "channels",
        "ObservationManifest.enrichments",
        "ChannelDeclaration.kind=HOOK",
        "ChannelDeclaration.kind=OTEL",
        "ChannelCapability.ownership=ENRICHMENT",
        "ChannelCapability.ownership=FALLBACK",
        "HookChannelManifest.declaration",
        "HookChannelManifest.bindings",
        "HookChannelManifest.installer",
        "HookChannelManifest.probe",
        "HookBinding.event",
        "HookBinding.signals",
        "HookBinding.signals=LIFECYCLE",
        "HookBinding.signals=TOOL",
        "HookBinding.delivery",
        "HookBinding.delivery=BEST_EFFORT",
        "HookBinding.delivery=ORDERED",
        "HookBinding.delivery=RETRIED",
        "HookBinding.correlation",
        "HookBinding.decoder",
        "CompatManifest.hook_events",
        "MockToolCall.name",
        "MockToolCall.arguments",
        "MockToolCall.call_id",
    ),
    _exempt(
        "available OTel channels have no live payload scenario and make qualification "
        "inconclusive; the channels family keeps their required obligation",
        "OtelChannelManifest.declaration",
        "OtelChannelManifest.protocol",
        "OtelChannelManifest.protocol=OTLP_HTTP_JSON",
        "OtelChannelManifest.protocol=OTLP_HTTP_PROTOBUF",
        "OtelChannelManifest.bindings",
        "OtelChannelManifest.correlation",
        "OtelChannelManifest.installer",
        "OtelBinding.name",
        "OtelBinding.signal",
        "OtelBinding.signal=LOGS",
        "OtelBinding.signals",
        "OtelBinding.signals=LIFECYCLE",
        "OtelBinding.signals=TOOL",
        "OtelBinding.correlation",
        "OtelBinding.decoder",
        "OtelCorrelation.auth_header",
        "OtelCorrelation.binding_attribute",
        "OtelCorrelation.channel_attribute",
        "OtelCorrelation.delivery_id_attribute",
        "OtelCorrelation.harness_attribute",
        "OtelCorrelation.participant_attribute",
    ),
    # --- runtime: native routes, reconnect, fallbacks
    _family(
        "runtime",
        "HarnessManifest.runtime",
        "RuntimeManifest.probe",
        "RuntimeManifest.plan",
        "RuntimeManifest.factory",
        "RuntimeManifest.channel",
        "RuntimeManifest.host",
        "RuntimeManifest.host=DETACHED_BACKEND",
        "RuntimeManifest.host=FRONTEND",
        "RuntimeManifest.frontend_installer",
        "RuntimeManifest.endpoint_discovery",
        "RuntimeManifest.runtime_credential",
        "RuntimeManifest.session_order",
        "RuntimeManifest.session_order=FRONTEND_FIRST",
        "RuntimeManifest.session_order=SESSION_FIRST",
        "RuntimeManifest.legacy_fallback",
        "RuntimeManifest.legacy_fallback=QUEUE_FOLLOWUP",
        "RuntimeManifest.legacy_fallback=SETTINGS_UPDATE",
        "RuntimeManifest.unavailable_capabilities",
        # Each derives runtime:unavailable-<name>; one with no public call to exercise it ends
        # inconclusive, never green.
        "RuntimeManifest.unavailable_capabilities=INTERRUPT",
        "RuntimeManifest.unavailable_capabilities=QUEUE_FOLLOWUP",
        "RuntimeManifest.unavailable_capabilities=SEND",
        "RuntimeManifest.unavailable_capabilities=SETTINGS_UPDATE",
        "LiveChannelDeclaration.channel",
        "LiveChannelDeclaration.drives_job_completion",
        "LiveChannelDeclaration.durable_fallback",
        "RuntimeEndpointDiscovery.parser",
        "RuntimeEndpointDiscovery.max_bytes",
        "RuntimeCredentialDeclaration.channel_id",
        "RuntimeCredentialDeclaration.env",
        "ChannelDeclaration.kind=LIVE",
        "ChannelDeclaration.kind=RUNTIME",
    ),
    # --- exemptions, each with the reason it stays true
    _exempt(
        "metadata checked by manifest validation; it changes no behaviour a live run could see",
        "HarnessManifest.api_version",
        "HarnessManifest.binaries",
        "HarnessManifest.aliases",
        "HarnessManifest.icon",
    ),
    _exempt(
        "model discovery fills the daemon's catalog and never a launch; the model family proves "
        "that a selection reaches the wire",
        "HarnessManifest.models",
        "ModelDiscoveryManifest.discoverer",
    ),
    _exempt(
        "policy validation proves version admission; the live suite temporarily admits only "
        "its supplied version so a closed gate cannot substitute for behavioral testing",
        "HarnessManifest.native_compatibility",
        "NativeCompatibilityManifest.probe",
        "NativeCompatibilityManifest.qualified_range",
    ),
    _exempt(
        "plugin-declared mutation choices are tooling checks of the suite, run separately from "
        "release qualification and checked against the generic mutation catalogue",
        "CompatManifest.canaries",
    ),
    _exempt(
        "mock acknowledgements for non-model side channels; protocol routes cannot be replaced "
        "and these requests never consume a scripted model reply",
        "CompatManifest.acknowledged_routes",
    ),
    _exempt(
        "stimulating a native child needs a CLI-specific task the mock cannot script generically",
        "ObservationManifest.lineage",
        "LineageManifest.native_children",
        "ChannelCapability.signal=LINEAGE",
    ),
    _exempt(
        "operator adoption of a terminal Theater did not start is not reachable through a spawn",
        "IdentityManifest.operator_candidate_admitter",
    ),
    _exempt(
        "timing and usage are accounting facts: the live suite has no oracle independent of the "
        "plugin's own parser, and the parsers are unit-tested against recorded transcripts",
        "ChannelCapability.signal=TIMING",
        "ChannelCapability.signal=USAGE",
    ),
    _exempt(
        "capability flags describing which facts exist; each fact is unit-tested per harness",
        "ObservationManifest.trajectory_capabilities",
        "TrajectoryCapabilities.supported",
        "TrajectoryCapabilities.unsupported",
        "TrajectoryCapabilities.observed",
        *(
            f"TrajectoryCapabilities.{flag}={feature}"
            for flag in ("supported", "unsupported", "observed")
            for feature in (
                "CONTEXT",
                "LIVE_UPDATES",
                "MODELS",
                "REASONING",
                "REQUESTS",
                "RETRIES",
                "TIMING",
                "TOOLS",
                "USAGE",
            )
        ),
    ),
    _exempt(
        "the channels scenario proves tool-lifecycle delivery and correlation; a fact of another "
        "signal has no oracle independent of the plugin's own decoder, which is unit-tested "
        "against recorded payloads",
        *(
            f"{binding}.signals={signal}"
            for binding in ("HookBinding", "OtelBinding")
            for signal in ("CONTENT", "IDENTITY", "LINEAGE", "MODEL", "TIMING", "TURN", "USAGE")
        ),
    ),
    _exempt(
        "queue and payload bounds are enforced and unit-tested in channel ingress, not by a CLI",
        "ChannelDeclaration.bounds",
        "ChannelBounds.max_queue",
        "ChannelBounds.max_payload_bytes",
        "OtelChannelManifest.bounds",
        "OtelBounds.max_attributes",
        "OtelBounds.max_records",
        "OtelBounds.max_text_bytes",
        "OtelBounds.max_value_depth",
    ),
    _exempt(
        "the foreground wait ceiling needs a multi-minute held call; covered by the MCP unit "
        "tests and revisited if a CLI changes its tool timeout",
        "McpRenderingManifest.foreground_wait",
        "McpForegroundWait.env",
        "McpForegroundWait.default_ms",
        "McpForegroundWait.margin_seconds",
    ),
    _exempt(
        "the Process channel kind has no declaring harness; it is unit-tested with fixtures",
        "ChannelDeclaration.kind=PROCESS",
    ),
    _exempt(
        "an unavailable channel wires nothing a CLI could exercise, and the day a plugin declares "
        "it available the channels family derives a required obligation",
        "HookChannelManifest.unavailable_reason",
        "OtelChannelManifest.unavailable_reason",
        "UnavailableChannelManifest.declaration",
        "UnavailableChannelManifest.reason",
    ),
)
