"""Fixed harness subsystem constants.

The kernel truncates ``pane_current_command`` at 15 characters
(TASK_COMM_LEN minus the NUL terminator), so binary names longer than
that arrive truncated and cannot be matched by exact comparison.
"""

from __future__ import annotations

#: Both tmux and Linux truncate process names to this many characters.
HARNESS_TMUX_OBSERVATION_NAME_LENGTH = 15
