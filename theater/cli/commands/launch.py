"""Default interactive launch for the bare ``theater`` command."""

from __future__ import annotations

from pathlib import Path

from theater.cli.commands.process import cmd_regie
from theater.cli.errors import BadUsage
from theater.client import call_sync
from theater.tmux import client as tmux
from theater.tmux.bootstrap import detach_current_client, launch_regie_session


def cmd_launch(args) -> int:
    """Ensure the daemon and open the régie in a tmux session."""
    if not tmux.available():
        raise BadUsage("tmux is not on PATH; Theater cannot launch the régie")
    call_sync("ping")
    if tmux.inside_tmux():
        result = cmd_regie(args)
        if result == 0:
            detach_current_client()
        return result
    try:
        launch_regie_session(str(Path.cwd()))
    except tmux.TmuxError as exc:
        raise BadUsage(
            f"could not open the régie tmux session: {exc}; check tmux and retry"
        ) from None
    return 0
