"""Shared contracts for Theater's package-manifest plugin kinds."""

from __future__ import annotations

#: One public manifest API shared by harness and MCP-server packages.
PLUGIN_API_VERSION = 1
MCP_PLUGIN_API_VERSION = PLUGIN_API_VERSION
MCP_SERVER_MANIFEST_API_VERSION = PLUGIN_API_VERSION

#: MCP-server manifest metadata and launch-plan bounds.
MCP_PLUGIN_DESCRIPTION_MAX_CHARS = 240
MCP_PLUGIN_CONFIG_MAX_FIELDS = 64
MCP_PLUGIN_LAUNCH_MAX_ARGV = 128
MCP_PLUGIN_LAUNCH_MAX_ENV = 128
MCP_PLUGIN_LAUNCH_MAX_ARTIFACTS = 64
MCP_PLUGIN_LAUNCH_MAX_TEXT_CHARS = 1_048_576
MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS = 16_384

#: Injected into every configured MCP-plugin sidecar.  The path names a
#: participant-private, core-written credential file; planners never see its bytes.
MCP_PLUGIN_CREDENTIAL_PATH_ENV = "THEATER_PLUGIN_CREDENTIAL_PATH"

#: Bounded credential-file and wire-token sizes keep malformed sidecars from
#: turning authentication into unbounded file or SQLite work.
MCP_PLUGIN_CREDENTIAL_MAX_CHARS = 512

#: One participant gets at most this many registry-omission audit events.
MCP_PLUGIN_SPAWN_OMISSION_MAX = 64
