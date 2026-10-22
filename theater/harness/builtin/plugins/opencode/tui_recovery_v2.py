"""Recover OpenCode 2.0.17-2.0.20 frames after OpenTUI leaves a viewport clipped."""

from __future__ import annotations

import json

from ._assets import read_asset

# Scoped on purpose: the wrapper patches OpenTUI renderer internals, so it stays
# a no-op outside releases whose @opentui/core build was checked. 2.0.17 and
# 2.0.20 pin the same @opentui/core 0.5.12 (identical lock hash) and an unchanged
# patch-diff.tsx as 2.0.18/2.0.19, so they inherit the clip-unwind defect;
# 2.0.6-2.0.16 build @opentui/core 0.5.10 with a different patch-diff.tsx.
_QUALIFIED_VERSIONS = ("2.0.17", "2.0.18", "2.0.19", "2.0.20")


def render_tui_recovery_v2(plugin_id: str) -> str:
    return (
        read_asset("tui_recovery_v2.js")
        .replace("__PLUGIN_ID__", json.dumps(plugin_id))
        .replace("__QUALIFIED_VERSIONS__", json.dumps(list(_QUALIFIED_VERSIONS)))
    )
