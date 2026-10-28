"""Rendered OpenCode JavaScript: settings preservation and recovery ordering."""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.builtin.plugins.opencode import (
    native_plugin,
    tui_recovery,
)


def test_native_settings_are_not_reexpanded() -> None:
    rendered = native_plugin.render_native_plugin(
        "__THEATER_SETTINGS__", Path("/x/config"), Path("/x/token"), "yolo"
    )
    source = next(text for path, text in rendered.items() if path.name == "server.js")
    assert '"participantID": "__THEATER_SETTINGS__"' in source


def test_recovery_keeps_ordered_replacement() -> None:
    recovery = tui_recovery.render_tui_recovery("__QUALIFIED_VERSIONS__")
    qualified = json.dumps(list(tui_recovery._QUALIFIED_VERSIONS))
    assert f'  id: "{qualified}",' in recovery
