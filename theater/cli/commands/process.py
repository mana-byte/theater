"""Daemon, MCP, and régie process entry commands."""

from __future__ import annotations

import asyncio
import sys

from theater import config


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

            delete_generation_file(generation_path(paths.daemon_stderr_logs_dir(), stderr_token))
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
    timing_log = getattr(args, "timing_log", None)
    timing = getattr(args, "timing", False) or obs.mcp_timing or timing_log is not None
    runtime_handle = configure(
        role=PROCESS_ROLE_MCP,
        otlp_enabled=obs.otlp_enabled,
        otlp_protocol=obs.otlp_protocol,
        otlp_endpoint=obs.otlp_endpoint,
        service_name=obs.service_name,
        export_interval_ms=obs.export_interval_ms,
        log_max_bytes=obs.log_max_bytes,
        log_backup_count=obs.log_backup_count,
        log_path=timing_log,
        foreground=timing and timing_log is None,
        timing=timing,
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
    """Reject the retired Theater-owned UI entry point without importing Textual."""
    del args
    print(
        "theater: `theater regie` has moved to the standalone `regie` command. "
        "Install the matching Régie package, then run `regie` (or `regie bridge start`).",
        file=sys.stderr,
    )
    return 1
