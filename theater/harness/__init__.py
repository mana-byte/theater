"""Harness registry — compatibility façade over `theater.harness.registry`.

Adapters are package manifests (`builtin/plugins/` and `$THEATER_HOME/plugins/`,
same contract, no built-in tier); the registry stays empty until `install` runs.
"""

from __future__ import annotations

import logging
import shutil  # noqa: F401 — tests monkeypatch theater.harness.shutil.which

from theater.config import ConfigError  # noqa: F401 — tests catch harness.ConfigError
from theater.constants.harness import (  # noqa: F401
    HARNESS_TMUX_OBSERVATION_NAME_LENGTH as _TMUX_TRUNCATION,
)
from theater.harness.base import (
    APPROVALS,
    MAX_TEXT,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchParameterSupport,
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
    TurnTerminal,
    clip,
    clipper,
    last_screen_line,
    status_after,
    theater_binary,
)
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelFact,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
    HookBinding,
    HookDeliveryMode,
    OtelBinding,
    OtelBounds,
    OtelCorrelation,
    OtelProtocol,
    OtelRecord,
    OtelSignal,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    PLUGIN_API_VERSION,
    ControlManifest,
    HarnessManifest,
    HookChannelManifest,
    IdentityManifest,
    InterruptPlan,
    LaunchManifest,
    LineageManifest,
    McpRenderingManifest,
    ModelDiscoveryManifest,
    NativeCompatibilityManifest,
    ObservationManifest,
    OtelChannelManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    LiveChannelDeclaration,
    NativeRequestId,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeFrontendConnection,
    RuntimeFrontendInstallContext,
    RuntimeFrontendInstaller,
    RuntimeFrontendOverlay,
    RuntimeHost,
    RuntimeIO,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimePlan,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
    validate_native_request_id,
)
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    Source,
    StreamPoint,
)
from theater.harness.loading.models import LoadedPlugin, PluginError
from theater.harness.manifests import (
    ManifestValidationError,
    compile_manifest,
    validate_manifest,
)
from theater.harness.registry import (  # noqa: F401
    _ALIASES,
    _BINARIES,
    _BROKEN,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)
from theater.harness.registry.capabilities import (
    check_model,
    check_reasoning,
    check_resume,
    overlay_mcp,
    plan_launch,
    supports_mcp_rendering,
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.harness.registry.claims import (  # noqa: F401
    _observation_keys_for,
)
from theater.harness.registry.claims import (  # noqa: F401
    binary_claim_keys as _binary_claim_keys,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_alias as _claim_alias,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_binary as _claim_binary,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_name as _claim_name,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_observation_keys as _claim_observation_keys,
)
from theater.harness.registry.claims import (  # noqa: F401
    release_claims as _release_claims,
)
from theater.harness.registry.claims import (  # noqa: F401
    unwrap_binary as _unwrap_binary,
)
from theater.harness.registry.install import (  # noqa: F401
    _reject,
    install,
)
from theater.harness.registry.lookup import (
    UNKNOWN_ICON,
    describe,
    get,
    harness_icon,
    known_binaries,
    native_compatibility_probe,
    native_compatibility_record,
    normalize,
    observation_lookup,
)
from theater.harness.registry.mcp import theater_mcp_servers
from theater.harness.transcript.observer import TranscriptObserver
from theater.harness.transcript.source import TranscriptSource

logger = logging.getLogger("theater.harness")

__all__ = [
    "APPROVALS",
    "HARNESSES",
    "MANIFEST_API_VERSION",
    "MAX_TEXT",
    "PLUGIN_API_VERSION",
    "SERVER_NAME",
    "UNKNOWN_ICON",
    "Attachment",
    "Batch",
    "CapabilityUnavailableReason",
    "ChannelBounds",
    "ChannelCapability",
    "ChannelDeclaration",
    "ChannelFact",
    "ChannelHealth",
    "ChannelHealthState",
    "ChannelKind",
    "ConnectionHealth",
    "ControlDeliveryPhase",
    "ControlKind",
    "ControlManifest",
    "ControlReceipt",
    "ControlTransport",
    "DeliveryResult",
    "Event",
    "EventKind",
    "EventPath",
    "Harness",
    "HarnessManifest",
    "HarnessObserver",
    "HarnessRuntime",
    "History",
    "HookBinding",
    "HookChannelManifest",
    "HookDeliveryMode",
    "IdentityManifest",
    "InterruptPlan",
    "LaunchManifest",
    "LaunchParameterSupport",
    "LaunchPlan",
    "LineageManifest",
    "LiveChannelDeclaration",
    "LoadedPlugin",
    "ManifestValidationError",
    "McpRenderingManifest",
    "ModelDiscoveryManifest",
    "NativeChild",
    "NativeCompatibilityManifest",
    "NativeRequestId",
    "NativeTurnOutcome",
    "NativeTurnTerminal",
    "ObservationManifest",
    "OtelBinding",
    "OtelBounds",
    "OtelChannelManifest",
    "OtelCorrelation",
    "OtelProtocol",
    "OtelRecord",
    "OtelSignal",
    "ParticipantObservationContext",
    "PluginError",
    "ResultCompleteness",
    "ResultProvenance",
    "ResumeLaunchOverlay",
    "RuntimeBinding",
    "RuntimeCapabilities",
    "RuntimeCompatibility",
    "RuntimeContext",
    "RuntimeFrontendConnection",
    "RuntimeFrontendInstallContext",
    "RuntimeFrontendInstaller",
    "RuntimeFrontendOverlay",
    "RuntimeHost",
    "RuntimeIO",
    "RuntimeLifecyclePhase",
    "RuntimeManifest",
    "RuntimePlan",
    "RuntimeSnapshot",
    "RuntimeWiring",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenManifest",
    "ScreenReading",
    "SessionOpenMode",
    "SignalKind",
    "SignalOwnership",
    "Source",
    "SourceManifest",
    "StreamPoint",
    "TranscriptObserver",
    "TranscriptSource",
    "TurnTerminal",
    "check_model",
    "check_reasoning",
    "check_resume",
    "clip",
    "clipper",
    "compile_manifest",
    "describe",
    "get",
    "harness_icon",
    "install",
    "known_binaries",
    "last_screen_line",
    "native_compatibility_probe",
    "native_compatibility_record",
    "normalize",
    "observation_lookup",
    "overlay_mcp",
    "plan_launch",
    "status_after",
    "supports_mcp_rendering",
    "supports_model",
    "supports_reasoning",
    "supports_resume",
    "theater_binary",
    "theater_mcp_servers",
    "validate_manifest",
    "validate_native_request_id",
]
