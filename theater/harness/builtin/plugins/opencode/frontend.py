"""Render the launch-local OpenCode TUI extension."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from theater import paths
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext, RuntimeFrontendOverlay
from theater.models import BadRequest

from ._assets import read_asset
from .frontend_inputs import TUI_INPUTS

_TUI_CONFIG = "theater-tui.json"
_TUI_PLUGIN = "theater-observer.mjs"
_SOCKET_SENTINEL = "__THEATER_FRONTEND_SOCKET__"
_TOKEN_SENTINEL = "__THEATER_FRONTEND_TOKEN__"


def install_opencode_tui_extension(
    context: RuntimeFrontendInstallContext,
) -> RuntimeFrontendOverlay:
    """Add the stock-UI extension without changing OpenCode's launch plan."""
    if os.environ.get("OPENCODE_TUI_CONFIG"):
        raise BadRequest("cannot compose the OpenCode TUI extension over OPENCODE_TUI_CONFIG")
    parsed = urlparse(context.endpoint)
    if parsed.scheme != "unix" or not parsed.path:
        raise BadRequest("OpenCode TUI extension requires a unix listener endpoint")
    root = paths.participant_observation_dir(context.participant_id, "opencode")
    config_path = root / _TUI_CONFIG
    plugin_path = root / _TUI_PLUGIN
    config = json.dumps({"plugin": [plugin_path.resolve().as_uri()]}, indent=2)
    return RuntimeFrontendOverlay(
        env={"OPENCODE_TUI_CONFIG": str(config_path)},
        files={
            config_path: config,
            plugin_path: render_opencode_tui_plugin(parsed.path, context.token_file),
        },
    )


def render_opencode_tui_plugin(socket_path: str, token_file: Path) -> str:
    return (
        read_asset("frontend.js")
        .replace("__THEATER_INPUT_OBSERVER__", TUI_INPUTS)
        .replace(_SOCKET_SENTINEL, json.dumps(socket_path))
        .replace(_TOKEN_SENTINEL, json.dumps(str(token_file)))
    )


__all__ = ["install_opencode_tui_extension", "render_opencode_tui_plugin"]
