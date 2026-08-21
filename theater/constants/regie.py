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
