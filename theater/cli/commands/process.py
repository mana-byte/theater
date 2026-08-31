"""Daemon, MCP, and régie process entry commands."""

from __future__ import annotations

import asyncio
import logging
import sys

from theater import config, paths
from theater.tmux import client as tmux


def cmd_daemon(args) -> int:
    from theater.daemon.lock import LockHeld
    from theater.daemon.server import DaemonRunOptions, run
    from theater.observability.logging import (
        delete_generation_file,
        generation_path,
        validate_token,
    )

    stderr_token = getattr(args, "stderr_token", None)
    if stderr_token is not None and not validate_token(stderr_token):
        print(f"theater: invalid stderr token: {stderr_token!r}", file=sys.stderr)
        return 2
    options = DaemonRunOptions(
        log_level=args.log_level,
        timing=getattr(args, "timing", False),
        stderr_token=stderr_token,
    )
    try:
        asyncio.run(run(options))
    except LockHeld as exc:
        if stderr_token is not None:
            from theater import paths

            delete_generation_file(generation_path(paths.logs_dir(), stderr_token))
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mcp(args) -> int:
    from theater.constants.observability import PROCESS_ROLE_MCP
    from theater.observability.runtime import configure

    settings = config.load()
    obs = settings.observability
    runtime_handle = configure(
        role=PROCESS_ROLE_MCP,
        otlp_enabled=obs.otlp_enabled,
        otlp_protocol=obs.otlp_protocol,
        otlp_endpoint=obs.otlp_endpoint,
        service_name=obs.service_name,
        export_interval_ms=obs.export_interval_ms,
    )
    try:
        from theater import harness as harness_registry

        harness_registry.install(settings)
        from theater.mcp.server import main

        main(args.participant_id, args.harness, args.toolset)
    finally:
        runtime_handle.shutdown()
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
    from theater.constants.observability import PROCESS_ROLE_REGIE
    from theater.observability.logging import log_unhandled_exceptions, prune_regie_generations
    from theater.observability.runtime import configure

    settings = config.load()
    obs = settings.observability
    paths.ensure_home()
    current_log = paths.regie_log_path()
    try:
        live_panes = asyncio.run(tmux.list_panes())
    except Exception as error:
        logging.getLogger("theater.regie").warning(
            "régie log pruning skipped: cannot discover live tmux panes: %s", error
        )
    else:
        try:
            prune_regie_generations(
                paths.regie_logs_dir(),
                current_log,
                (pane.pane_id for pane in live_panes),
            )
        except OSError as error:
            logging.getLogger("theater.regie").warning("régie log pruning failed: %s", error)
    runtime_handle = configure(
        role=PROCESS_ROLE_REGIE,
        otlp_enabled=obs.otlp_enabled,
        otlp_protocol=obs.otlp_protocol,
        otlp_endpoint=obs.otlp_endpoint,
        service_name=obs.service_name,
        export_interval_ms=obs.export_interval_ms,
        log_max_bytes=obs.log_max_bytes,
        log_backup_count=obs.log_backup_count,
        log_path=paths.regie_log_path(),
    )
    regie_logger = logging.getLogger("theater.regie")
    try:
        with log_unhandled_exceptions(regie_logger, "régie"):
            regie_logger.info("régie starting")
            from theater import harness as harness_registry

            harness_registry.install(settings)
            from theater.regie.app import run_regie

            run_regie(settings)
            regie_logger.info("régie stopped")
    finally:
        runtime_handle.shutdown()
    return 0
