"""Daemon, MCP, and régie process entry commands."""

from __future__ import annotations

import asyncio
import logging
import sys

from theater import config
from theater.tmux import client as tmux


def cmd_daemon(args) -> int:
    from theater import timing
    from theater.daemon.server import run

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    if getattr(args, "timing", False):
        timing.enable_trace()
    try:
        asyncio.run(run())
    except RuntimeError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mcp(args) -> int:
    from theater.mcp.server import main

    main(args.participant_id, args.harness)
    return 0


def cmd_regie(args) -> int:
    """Launch the régie TUI.

    Must be run inside tmux: the régie is itself a tmux pane, and the stage
    is a real pane in the same window. If $TMUX is not set, the user needs
    to attach to a session first.
    """
    if not tmux.inside_tmux():
        print(
            "theater: regie must run inside tmux — attach to a session first",
            file=sys.stderr,
        )
        return 1
    settings = config.load()
    from theater.regie.app import run_regie

    run_regie(settings)
    return 0
