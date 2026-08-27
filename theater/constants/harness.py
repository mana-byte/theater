"""Immutable constants for harness discovery and spawned harness processes."""

from __future__ import annotations

from theater.constants.tmux import TMUX_DEFAULT_SESSION

#: Name the theater MCP server is registered under inside each harness.
HARNESS_MCP_SERVER_NAME = "theater"

#: Per-tool MCP call timeout; 340 = MAX_AWAIT (300s) + 40s client slack. Duplicated, not imported.
HARNESS_MCP_TOOL_TIMEOUT_SECONDS = 340.0

#: No default anywhere: the caller must choose — the whole safety story.
HARNESS_APPROVAL_POLICIES = ("manual", "edits", "yolo")

#: Supported immutable harness-manifest schema version.
HARNESS_MANIFEST_API_VERSION = 1

#: Compatibility name for the public manifest plugin API version.
HARNESS_PLUGIN_API_VERSION = HARNESS_MANIFEST_API_VERSION

#: Bus text clip limit; the transcript on disk remains the full record.
HARNESS_EVENT_TEXT_MAX_CHARS = 2000

#: Session name when no tmux session is requested or found.
SPAWN_FALLBACK_TMUX_SESSION = TMUX_DEFAULT_SESSION

#: Poll attempts when confirming a pane is gone after kill-pane.
SPAWN_KILL_POLL_ATTEMPTS = 5

#: Interval between kill-pane confirmation polls, in seconds.
SPAWN_KILL_POLL_INTERVAL_SECONDS = 0.25

#: Both tmux and Linux truncate observed process names to this length.
HARNESS_TMUX_OBSERVATION_NAME_LENGTH = 15

#: Read-chunk size for the initial transcript scan in attach_point.
HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES = 1 << 20

#: Default bounded capacity for a future harness signal channel.
HARNESS_CHANNEL_DEFAULT_MAX_QUEUE = 128

#: Default bounded native payload size for a future harness signal channel.
HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES = 65_536

#: Longest stable channel identifier accepted by the generic contracts.
HARNESS_CHANNEL_ID_MAX_CHARS = 64

#: Longest native event or delivery identifier accepted by hook ingress.
HARNESS_HOOK_IDENTIFIER_MAX_CHARS = 128

#: Maximum participant-scoped hook credential length, excluding its trailing newline.
HARNESS_HOOK_TOKEN_MAX_CHARS = 128

#: Maximum JSON object depth accepted from a native hook.
HARNESS_HOOK_MAX_JSON_DEPTH = 8

#: Maximum object attributes accepted from a native hook.
HARNESS_HOOK_MAX_JSON_ATTRIBUTES = 64

#: Maximum queue capacity a manifest may request for one hook channel.
HARNESS_HOOK_MAX_QUEUE = 4096

#: Maximum native hook JSON body a manifest may request.
HARNESS_HOOK_MAX_PAYLOAD_BYTES = HARNESS_CHANNEL_DEFAULT_MAX_PAYLOAD_BYTES

#: Maximum bounded delivery identities retained per hook inbox.
HARNESS_HOOK_DEDUPE_MAX_DELIVERIES = 4096

#: Maximum simultaneous synchronous callback executions per hook runtime.
HARNESS_HOOK_CALLBACK_MAX_IN_FLIGHT = 8

#: Bound on one synchronous native correlation callback.
HARNESS_HOOK_CORRELATION_TIMEOUT_SECONDS = 1.0

#: Bound on one synchronous native decoder callback.
HARNESS_HOOK_DECODER_TIMEOUT_SECONDS = 1.0

#: Runtime health retains only this many recent diagnostics per channel.
HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS = 8

#: Individual runtime channel diagnostics remain bounded and display-safe.
HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS = 240

#: Default timeout for bounded enrichment source reads.
HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS = 5.0

#: Maximum trajectory facts retained for dedupe across children and polls.
HARNESS_DEDUPE_MAX_FACTS = 4096

# Compatibility alias re-exported by the spawner façade.
FALLBACK_SESSION = SPAWN_FALLBACK_TMUX_SESSION
