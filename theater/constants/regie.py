"""Immutable régie UI values: leaf geometry, send style, spinner frames, rail glyphs.

Stdlib-only so Textual stays off non-régie import paths.
"""

from __future__ import annotations

#: Rows drawn per participant leaf. The rail grid indexes in these.
REGIE_TREE_LEAF_ROWS = 3

#: A bright style for the heavy line glyph under a send travelling the rails.
REGIE_SEND_TRACE_STYLE = "$accent bold"

#: Braille spinner frames, matching vibe exactly. U+28xx is unambiguously narrow.
REGIE_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Ten grayscale frames so the working harness pulse stays in lockstep with the spinner.
REGIE_WORKING_HARNESS_STYLES = (
    "#FFFFFF",
    "#EFEFEF",
    "#DFDFDF",
    "#CFCFCF",
    "#BFBFBF",
    "#AFAFAF",
    "#BFBFBF",
    "#CFCFCF",
    "#DFDFDF",
    "#EFEFEF",
)

#: Box-drawing pieces for the lineage rails.
REGIE_TREE_BRANCH = "├── "
REGIE_TREE_LAST_BRANCH = "└── "
REGIE_TREE_RAIL = "│   "
REGIE_TREE_GAP = "    "

#: How often a travelling tree trace moves (seconds). Slower than the spinners.
REGIE_TRACE_ANIM_INTERVAL = 0.10

#: Ceiling on concurrent traces; more is noise, not signal.
REGIE_MAX_TRACE_ANIMS = 6

#: Ceiling on concurrent await pulses; more turns the tree into fog.
REGIE_MAX_AWAIT_ANIMS = 12

#: Max seconds an await pulse may run without its end row before expiring.
REGIE_AWAIT_ANIM_TTL = 330.0

#: Footer value animation tick interval in seconds.
REGIE_FOOTER_ANIM_INTERVAL = 0.1

#: Footer value animation total duration in seconds.
REGIE_FOOTER_ANIM_DURATION = 2.0

#: Number of frames in one footer animation cycle.
REGIE_FOOTER_ANIM_FRAMES = round(REGIE_FOOTER_ANIM_DURATION / REGIE_FOOTER_ANIM_INTERVAL)

#: Maximum rows the usage breakdown overlay may cover.
REGIE_USAGE_BREAKDOWN_MAX_HEIGHT = 12

#: Stable key for the placeholder shown when the tree is empty.
REGIE_EMPTY_TREE_KEY = ("empty", "")

#: Empty-tree call-to-action shortcut text.
REGIE_EMPTY_TREE_SHORTCUT = "Ctrl+P"
#: Style for the empty-tree shortcut text.
REGIE_EMPTY_TREE_SHORTCUT_STYLE = "$text-accent bold"
#: Tail appended after the shortcut in the empty-tree hint.
REGIE_EMPTY_TREE_TAIL = " to get started"
#: Full empty-tree hint, shortcut plus tail.
REGIE_EMPTY_TREE_HINT = f"{REGIE_EMPTY_TREE_SHORTCUT}{REGIE_EMPTY_TREE_TAIL}"

#: Leaf spinner/animation timer interval in seconds.
REGIE_LEAF_SPINNER_INTERVAL = 0.1

#: Delay between startup typing frames, in seconds.
REGIE_STARTUP_REVEAL_INTERVAL_SECONDS = 0.035

#: Visible columns added to each leaf on a startup typing frame.
REGIE_STARTUP_REVEAL_COLUMNS_PER_FRAME = 1

#: Visible columns added to a newly discovered leaf on each frame.
REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME = 5

#: Frames between the start of adjacent leaves.
REGIE_STARTUP_REVEAL_STAGGER_FRAMES = 2

#: Hard deadline for the one-shot startup animation.
REGIE_STARTUP_REVEAL_MAX_SECONDS = 4.0

#: Skip startup animation when an unusually large tree would repaint too much.
REGIE_STARTUP_REVEAL_MAX_LEAVES = 100

#: Suppresses the tree highlight while the footer owns the keyboard cursor.
REGIE_HIDDEN_TREE_CURSOR = -1

#: Footer keyboard navigation: left arrow mapping.
REGIE_USAGE_METRIC_LEFT = {"output": "input", "cache": "output", "average": "cost"}
#: Footer keyboard navigation: right arrow mapping.
REGIE_USAGE_METRIC_RIGHT = {"input": "output", "output": "cache", "cost": "average"}
#: Footer keyboard navigation: down arrow mapping.
REGIE_USAGE_METRIC_DOWN = {"input": "cost", "output": "average", "cache": "average"}
#: Footer keyboard navigation: up arrow mapping.
REGIE_USAGE_METRIC_UP = {"cost": "input", "average": "cache"}

#: How often the régie polls the daemon for usage data (seconds).
REGIE_USAGE_POLL_INTERVAL_SECONDS = 10.0

#: Maps [regie] cost_window config values to hours for the usage RPC.
REGIE_COST_WINDOW_HOURS: dict[str, float] = {
    "day": 24.0,
    "week": 168.0,
    "month": 720.0,
    "year": 8760.0,
}

#: Maps [regie] cost_window config values to current-period display labels.
REGIE_COST_WINDOW_LABELS: dict[str, str] = {
    "day": "today",
    "week": "this week",
    "month": "this month",
    "year": "this year",
}

#: Maps [regie] cost_window config values to rolling-period display labels.
REGIE_COST_WINDOW_ROLLING_LABELS: dict[str, str] = {
    "day": "last 24h",
    "week": "last 7d",
    "month": "last 30d",
    "year": "last 365d",
}
