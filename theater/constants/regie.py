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

#: Minimum width reserved for usage labels before terminal ellipsis.
REGIE_USAGE_BREAKDOWN_LABEL_MIN_WIDTH = 7
#: Fixed width for each right-aligned usage period cell.
REGIE_USAGE_BREAKDOWN_NUMERIC_WIDTH = 8
#: Rich table cell padding used by compact and detailed usage tables.
REGIE_USAGE_BREAKDOWN_TABLE_PADDING = (0, 1)
#: Indentation applied to model rows beneath a harness summary.
REGIE_USAGE_BREAKDOWN_MODEL_INDENT = "  "
#: Blank rows inserted between detailed harness groups.
REGIE_USAGE_BREAKDOWN_GROUP_SPACER_ROWS = 1
#: Rich row styles for the detailed hierarchy.
REGIE_USAGE_BREAKDOWN_HARNESS_STYLE = "bold"
REGIE_USAGE_BREAKDOWN_MODEL_STYLE = "dim"
REGIE_USAGE_BREAKDOWN_TOTAL_STYLE = "bold"
#: Marker for a usage row whose historical model attribution was nullable.
REGIE_USAGE_BREAKDOWN_UNKNOWN_MODEL_MARKER = "†"

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

#: Visible columns added to an agent-spawned leaf on each frame.
REGIE_NEW_LEAF_REVEAL_COLUMNS_PER_FRAME = 5

#: Frames between the start of adjacent leaves.
REGIE_STARTUP_REVEAL_STAGGER_FRAMES = 2

#: Hard deadline for the one-shot startup animation.
REGIE_STARTUP_REVEAL_MAX_SECONDS = 4.0

#: Skip startup animation when an unusually large tree would repaint too much.
REGIE_STARTUP_REVEAL_MAX_LEAVES = 100

#: Suppresses the tree highlight while the footer owns the keyboard cursor.
REGIE_HIDDEN_TREE_CURSOR = -1

#: Hidden Textual binding targeted by the tmux return command.
REGIE_RETURN_SIGNAL_TEXTUAL = "ctrl+g"
#: tmux spelling for the same control key.
REGIE_RETURN_SIGNAL_TMUX = "C-g"

#: Textual system-command title replaced by the dashboard tips.
REGIE_PALETTE_KEYS_COMMAND_TITLE = "Keys"

#: Availability markers and styles for the dashboard's compact harness list.
REGIE_DASHBOARD_HARNESS_AVAILABLE_GLYPH = "✓"
REGIE_DASHBOARD_HARNESS_UNAVAILABLE_GLYPH = "✗"
REGIE_DASHBOARD_HARNESS_AVAILABLE_STYLE = "$success"
REGIE_DASHBOARD_HARNESS_UNAVAILABLE_STYLE = "$text-muted"

#: Style for highlighted words in the dashboard sentence animation.
REGIE_DASHBOARD_HIGHLIGHT_STYLE = "$accent bold"

#: Rectangular block glyph used by cycling dashboard text.
REGIE_DASHBOARD_CURSOR_GLYPH = "█"
#: Distinct theme-aware style for the main dashboard cursor.
REGIE_DASHBOARD_CURSOR_STYLE = "$secondary"
#: Dim cursor style used by the dashboard tip animation.
REGIE_DASHBOARD_TIP_CURSOR_STYLE = "$text-muted dim"
#: Base style for dashboard tip text.
REGIE_DASHBOARD_TIP_STYLE = "$text-muted dim"
#: Accent style for actionable fragments in dashboard tips.
REGIE_DASHBOARD_TIP_HIGHLIGHT_STYLE = "$text-accent bold"

_H = REGIE_DASHBOARD_HIGHLIGHT_STYLE

#: Sentences cycled in the centered dashboard; each has at least one highlighted part.
REGIE_DASHBOARD_SENTENCES: tuple[tuple[str | tuple[str, str], ...], ...] = (
    (("imagine", _H), " writing code that reads like prose"),
    (("building", _H), " tools that build tools is recursion worth chasing"),
    ("the best ", ("debugger", _H), " is a good night's sleep"),
    ("every function is a ", ("promise", _H), "; every type is a contract"),
    (("simplicity", _H), " is the ultimate sophistication in software"),
    (("imagine", _H), " a world where tests write themselves"),
    (("building", _H), " software is an act of empathy for future readers"),
    ("the ", ("compiler", _H), " is your friend, not your enemy"),
    (("naming", _H), " things is hard; unnamed things are harder"),
    ("good ", ("abstractions", _H), " earn their weight; bad ones exact it"),
    (("imagine", _H), " pairing with someone who always reads the docs"),
    (("building", _H), " is a conversation between intent and constraint"),
    ("every bug is a ", ("feature", _H), " you did not understand yet"),
    ("the ", ("REPL", _H), " is a lab bench for ideas"),
    (("imagine", _H), " if every error message apologized"),
    (("building", _H), " systems that heal themselves is not a dream"),
    (("complexity", _H), " is a debt you pay with interest"),
    ("the ", ("function", _H), " is the unit of thought"),
    (("imagine", _H), " a type system that catches your typos"),
    (("building", _H), " software is gardening, not architecture"),
    ("a good ", ("test suite", _H), " is a love letter to your future self"),
    (("imagine", _H), " refactoring without fear"),
    (("building", _H), " software is a series of small wins"),
    ("the ", ("terminal", _H), " is a musical instrument; learn to play it"),
    (("imagine", _H), " a codebase where every file earns its keep"),
    (("building", _H), " software is a craft, not an assembly line"),
    ("the best code is no code; the second best is ", ("small", _H), " code"),
    (("imagine", _H), " a pull request that teaches you something"),
    (("building", _H), " trust in software means building tests"),
    ("every ", ("interface", _H), " is a story you tell the caller"),
    (("imagine", _H), " if documentation wrote itself"),
    (("building", _H), " is how programmers think out loud"),
)
del _H

_D = REGIE_DASHBOARD_TIP_STYLE
_T = REGIE_DASHBOARD_TIP_HIGHLIGHT_STYLE

#: Cycling tips covering the régie's user-facing controls and discovery surfaces.
REGIE_DASHBOARD_TIPS: tuple[tuple[str | tuple[str, str], ...], ...] = (
    (("Tips: use ", _D), ("j/k or ↑/↓", _T), (" to move through the agent tree", _D)),
    (
        ("Tips: press ", _D),
        ("j or ↓", _T),
        (" past the last agent to enter usage stats", _D),
    ),
    (
        ("Tips: use ", _D),
        ("h/j/k/l or arrows", _T),
        (" to move through usage stats", _D),
    ),
    (("Tips: press ", _D), ("k or ↑", _T), (" from the top stats row to return", _D)),
    (("Tips: press ", _D), ("Enter", _T), (" to stage or unstage an agent", _D)),
    (
        ("Tips: outside usage stats, ", _D),
        ("l", _T),
        (" stages and focuses the selected agent", _D),
    ),
    (
        ("Tips: when the key is free, Theater binds ", _D),
        ("<prefix> h", _T),
        (" to return from the stage or trajectory", _D),
    ),
    (
        ("Tips: ", _D),
        ("single-click", _T),
        (" an agent to select it; ", _D),
        ("double-click", _T),
        (" to stage it", _D),
    ),
    (
        ("Tips: hover a ", _D),
        ("usage tile", _T),
        (" for per-harness stats for today, this week, and this month", _D),
    ),
    (
        ("Tips: ", _D),
        ("click a usage tile", _T),
        (" or press ", _D),
        ("Enter", _T),
        (" in the footer to toggle per-model details", _D),
    ),
    (("Tips: press ", _D), ("Ctrl+P", _T), (" to open the command palette", _D)),
    (
        ("Tips: use the ", _D),
        ("palette", _T),
        (" to spawn any available harness", _D),
    ),
    (
        ("Tips: use the ", _D),
        ("palette", _T),
        (" to resume a compatible dead session", _D),
    ),
    (
        ("Tips: use the ", _D),
        ("palette", _T),
        (" to show or hide inter-agent bus traffic", _D),
    ),
    (("Tips: use the ", _D), ("palette", _T), (" to change the régie theme", _D)),
    (("Tips: use the ", _D), ("palette", _T), (" to save an SVG screenshot", _D)),
    (("Tips: press ", _D), ("x", _T), (" to kill the selected managed session", _D)),
    (
        ("Tips: press ", _D),
        ("q", _T),
        (" to exit; the daemon and agent sessions keep running", _D),
    ),
    (
        ("Tips: set ", _D),
        ("regie.cost_window", _T),
        (" to day, week, month, or year", _D),
    ),
    (("Tips: ", _D), ("click this tip", _T), (" to show the next one", _D)),
)
del _D, _T

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
