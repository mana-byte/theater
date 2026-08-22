"""Immutable tmux command, formatting, and compatibility values."""

from __future__ import annotations

#: Shared fallback session for the dashboard and daemon-created agent windows.
TMUX_DEFAULT_SESSION = "theater"

#: Name shown for the singleton régie window created by the bare CLI.
TMUX_REGIE_WINDOW_NAME = "régie"

#: Window-local marker used to find an existing régie without matching titles.
TMUX_REGIE_WINDOW_OPTION = "@theater_regie"

#: Marker value written to the régie window option.
TMUX_REGIE_WINDOW_OPTION_VALUE = "1"

# Ceiling for one local tmux subprocess before it is considered wedged.
TMUX_RUN_TIMEOUT_SECONDS = 10.0

# U+241E cannot occur in tmux format fields and safely separates pane values.
TMUX_FIELD_SEPARATOR = "\u241e"

# Exact list-panes projection used to construct Pane records.
TMUX_PANE_FORMAT = (
    f"#{{pane_id}}{TMUX_FIELD_SEPARATOR}#{{pane_pid}}{TMUX_FIELD_SEPARATOR}"
    f"#{{pane_current_path}}{TMUX_FIELD_SEPARATOR}#{{window_id}}{TMUX_FIELD_SEPARATOR}"
    f"#{{session_name}}{TMUX_FIELD_SEPARATOR}#{{window_name}}{TMUX_FIELD_SEPARATOR}"
    f"#{{pane_current_command}}"
)

# tmux 3.7 introduced vis(3) paste escaping; -S restores raw bytes.
TMUX_RAW_PASTE_MIN_VERSION = (3, 7)

# tmux 3.7 alone needs the break-pane naming workaround fixed in 3.7a.
TMUX_BREAK_PANE_WORKAROUND_VERSION = "3.7"

# Per-pane buffer prefix avoids collisions between concurrent deliveries.
TMUX_PASTE_BUFFER_PREFIX = "theater-"

# Placeholder required by the tmux 3.7 break-pane workaround.
TMUX_BREAK_PANE_PLACEHOLDER_NAME = "theater"
