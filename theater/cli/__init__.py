"""Command line entry point.

Three audiences, one binary:

    theater daemon      the singleton, usually started implicitly by a client
    theater mcp          the per-agent stdio MCP server, started by a harness
    theater ls|spawn    a human at a terminal

Parser construction lives in ``cli/parser.py``; pure formatting in
``cli/render.py``; command implementations in ``cli/commands/``.  This module
is the compatibility façade and ``main`` entry point.
"""

from __future__ import annotations

import shutil  # noqa: F401 — tests monkeypatch cli.shutil.which
import sys

from theater import config, paths
from theater import harness as harness_registry
from theater.cli.commands import COMMANDS

# Re-export command functions for tests that call them directly.
from theater.cli.commands.bus import (  # noqa: F401
    _emit_bus,
    _follow_bus,
    cmd_bus,
)
from theater.cli.commands.identity import (  # noqa: F401
    _hook_string,
    _send_transcript_receipt,
    cmd_bind,
    cmd_candidates,
    cmd_claude_receipt,
    cmd_transcript_receipt,
)
from theater.cli.commands.introspection import (  # noqa: F401
    _harness_rows,
    _print_coverage,
    cmd_config,
    cmd_harnesses,
    cmd_models,
    cmd_stats,
)
from theater.cli.commands.maintenance import (  # noqa: F401
    _await_daemon_gone,
    _daemon_released,
    _shutdown_running_daemon,
    cmd_gc,
    cmd_restart,
    cmd_stop,
)
from theater.cli.commands.participants import (  # noqa: F401
    _spawn_harness,
    _watch_ls,
    cmd_adopt,
    cmd_kill,
    cmd_ls,
    cmd_name,
    cmd_spawn,
)
from theater.cli.commands.process import cmd_daemon, cmd_mcp, cmd_regie  # noqa: F401
from theater.cli.errors import BadUsage
from theater.cli.parser import (  # noqa: F401
    _add_gc_parser,
    _add_models_parser,
    _add_name_parser,
    _add_process_parsers,
    _add_receipt_parser,
    _parser,
)
from theater.cli.render import (  # noqa: F401
    _bus_line,
    _candidate_line,
    _format_bytes,
    _format_floor,
    _format_ls,
    _matching,
    _row_line,
    _width,
)
from theater.cli.render import (
    _models_block as _render_models_block,
)
from theater.client import DaemonClient, call_sync  # noqa: F401
from theater.constants.cli import (
    CLI_CLEAR_SCREEN as _CLEAR,  # noqa: F401
)
from theater.constants.cli import (
    CLI_FOLLOW_BATCH_SIZE as _FOLLOW_BATCH,  # noqa: F401
)
from theater.constants.cli import (
    CLI_STOP_TIMEOUT_SECONDS as STOP_TIMEOUT,  # noqa: F401
)
from theater.harness import (
    HARNESSES,  # noqa: F401
    harness_icon,  # noqa: F401
)
from theater.observability.runtime import ObservabilityError
from theater.protocol import RemoteError
from theater.tmux import client as tmux  # noqa: F401

_COMMANDS = COMMANDS


def _models_block(harness: str, models: list[str]) -> str:
    """Render a `[models]` entry, using the config section name."""
    return _render_models_block(harness, models, config.MODELS_SECTION)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths.ensure_home()
    # Long-lived process commands own their harness install and observability setup.
    _process_commands = frozenset({None, "daemon", "mcp", "regie"})
    try:
        # Build the harness registry before the command runs; process commands are exempt.
        if args.command not in _process_commands and args.command != "config":
            harness_registry.install(config.load())
        return _COMMANDS[args.command](args)
    except (BadUsage, config.ConfigError, ObservabilityError) as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except RemoteError as exc:
        print(f"theater: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except ConnectionError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # `ls --watch` and `bus -f` are meant to be ended this way.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
