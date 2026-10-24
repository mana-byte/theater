"""Recover OpenCode 2.0.18–2.0.20 frames after OpenTUI leaves a viewport clipped."""

from __future__ import annotations

import json

from ._assets import read_asset

# Scoped on purpose: the wrapper patches OpenTUI renderer internals, so it stays
# a no-op outside checked releases sharing the affected @opentui/core 0.5.12 build.
_QUALIFIED_VERSIONS = ("2.0.18", "2.0.19", "2.0.20")


def render_tui_recovery(plugin_id: str) -> str:
    return (
        read_asset("tui_recovery.js")
        .replace("__PLUGIN_ID__", json.dumps(plugin_id))
        .replace("__QUALIFIED_VERSIONS__", json.dumps(list(_QUALIFIED_VERSIONS)))
    )
