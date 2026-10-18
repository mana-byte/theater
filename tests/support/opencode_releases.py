"""Release strings derived from the qualified OpenCode windows, so tests never pin a real tag."""

from __future__ import annotations

from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_V2_MAX_VERSION,
    OPENCODE_SERVER_V2_MIN_VERSION,
    OPENCODE_TUI_MIN_VERSION,
    release_text,
)

V1_RELEASE = release_text(OPENCODE_TUI_MIN_VERSION)  # inside the qualified 1.x window
V2_RELEASE = release_text(OPENCODE_SERVER_V2_MIN_VERSION)  # inside the qualified 2.x window
V2_UNQUALIFIED = release_text(OPENCODE_SERVER_V2_MAX_VERSION)  # the first release past it
