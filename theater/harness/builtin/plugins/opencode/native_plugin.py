"""Launch-local OpenCode 2.x plugin: session receipts, approval enforcement, and MCP catalog.

2.x loads configured plugins only as directories exporting a default `{id, setup}`
(core/src/config/plugin/source.ts, core/src/plugin/module.ts), so this renders a small package.
"""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.base import theater_binary

from ._assets import read_asset
from .approval import approval_ruleset
from .constants import (
    APPROVAL_EXEMPT_ACTIONS,
    MCP_CATALOG_MAX_BYTES,
    MCP_CATALOG_MAX_SERVERS,
    MCP_CATALOG_MAX_TOOLS,
    MCP_CATALOG_NAME_MAX_BYTES,
    MCP_CATALOG_VERSION,
    RECEIPT_RETRY_DELAYS_MS,
)
from .mcp import catalog_path
from .tui_recovery import render_tui_recovery

PLUGIN_ID = "theater.opencode-session"
_PACKAGE = {"name": "theater-opencode-session", "private": True, "type": "module"}
_SETTINGS_SENTINEL = "__THEATER_SETTINGS__"


def plugin_dir(config_path: Path) -> Path:
    return config_path.with_suffix(".opencode")


def render_native_plugin(
    participant_id: str, config_path: Path, token_path: Path, approval: str
) -> dict[Path, str]:
    """The plugin package's files; yolo renders no rules and so registers no approval hook."""
    settings = {
        "pluginID": PLUGIN_ID,
        "participantID": participant_id,
        "tokenPath": str(token_path),
        "theater": theater_binary(),
        "retryDelays": list(RECEIPT_RETRY_DELAYS_MS),
        "rules": [dict(rule) for rule in approval_ruleset(approval)],
        "exemptActions": list(APPROVAL_EXEMPT_ACTIONS),
        "catalog": {
            "path": str(catalog_path(participant_id)),
            "version": MCP_CATALOG_VERSION,
            "maxBytes": MCP_CATALOG_MAX_BYTES,
            "maxServers": MCP_CATALOG_MAX_SERVERS,
            "maxTools": MCP_CATALOG_MAX_TOOLS,
            "nameMaxBytes": MCP_CATALOG_NAME_MAX_BYTES,
        },
    }
    directory = plugin_dir(config_path)
    return {
        directory / "package.json": json.dumps(_PACKAGE, indent=2),
        directory / "server.js": read_asset("native_plugin.js").replace(
            _SETTINGS_SENTINEL, json.dumps(settings)
        ),
        directory / "tui.js": render_tui_recovery(PLUGIN_ID),
    }


__all__ = ["PLUGIN_ID", "plugin_dir", "render_native_plugin"]
