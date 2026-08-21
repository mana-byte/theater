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
