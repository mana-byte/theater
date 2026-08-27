"""Vibe-native names, markers, and bounds."""

from __future__ import annotations

VIBE_MCP_SERVERS_ENV = "VIBE_MCP_SERVERS"
VIBE_ACTIVE_MODEL_ENV = "VIBE_ACTIVE_MODEL"
VIBE_SESSION_LOGGING_SAVE_DIR_ENV = "VIBE_SESSION_LOGGING__SAVE_DIR"
VIBE_ACTIVE_MODEL_CONFIG_KEY = "active_model"
VIBE_HOME_ENV = "VIBE_HOME"
VIBE_CONFIG_FILENAME = "config.toml"

MESSAGES_FILENAME = "messages.jsonl"
META_FILENAME = "meta.json"
SESSION_DIRECTORY_PREFIX = "session_"

IDLE_PROMPTS = ("❯", "❯ ", "> ❯")
_SCREEN_IDLE_PROMPTS = (*IDLE_PROMPTS, ">")
APPROVAL_MARKER = "Esc reject"
WORKING_MARKER = "to interrupt"
WORKING_MARKER_KEY = "Esc"
TRUST_MARKER = "Malicious configs can modify"
_SCREEN_TAIL_LINES = 6
_SPINNER_TAIL_LINES = 8

_SCAN_LIMIT = 200

ISOLATION_MARKER = ".theater-vibe-source"
_MARKER_VERSION = 1
_MARKER_KEY = "vibe-domain-marker.key"

_WRITE_TOOLS: dict[str, str] = {
    "write_file": "file_path",
    "edit": "file_path",
}
_READ_TOOLS: dict[str, str] = {
    "read_file": "file_path",
}
