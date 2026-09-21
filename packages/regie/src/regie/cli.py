"""Standalone Régie and persistent tmux-bridge command line ownership."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from regie.config import SettingsError, load_settings
from regie.constants import REGIE_REQUIRED_CAPABILITIES
from regie.observability import LoggingHandle, configure_logging, prune_regie_generations
from regie.paths import RegiePathError, RegiePaths, paths_from_environment
from regie.process import (
    BridgeProcessManager,
    BridgeProcessStatus,
    RegieStartupError,
    connect_or_start_daemon,
    run_bridge_worker,
)
from regie.tmux import bootstrap as tmux_bootstrap
from regie.tmux.command import TmuxError
from theater.frontend import FrontendClient


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "_bridge-worker":
        return asyncio.run(_worker(args))
    paths = paths_from_environment()
    socket_path = args.socket or paths.daemon_socket
    manager = BridgeProcessManager(paths, socket_path=socket_path)
    log_handle = None
    try:
        if args.command == "bridge" and args.bridge_command in {"status", "stop"}:
            return _bridge_command(args, paths, socket_path, manager)
        paths.ensure_private_runtime()
        if args.command == "bridge":
            log_handle = configure_logging(paths.ui_log_path)
            return _bridge_command(args, paths, socket_path, manager)
        _require_tmux_available()
        settings = load_settings(paths.config_path)
        _probe_daemon(paths, socket_path, args.client_id)
        bridge = manager.start()
        server_identity = _bridge_server_identity(bridge)
        if tmux_bootstrap.current_pane_id() is None:
            log_handle = configure_logging(paths.ui_log_path)
            tmux_bootstrap.launch_regie_session(
                str(Path.cwd()),
                command=_regie_command(socket_path, args.client_id),
                expected_server_identity=server_identity,
            )
            return 0
        log_handle = _configure_ui_logging(paths, server_identity)
        asyncio.run(tmux_bootstrap.require_current_pane(server_identity))
        asyncio.run(tmux_bootstrap.sync_color_environment(server_identity))
        _run_app(socket_path, args.client_id, settings, server_identity)
        tmux_bootstrap.detach_current_client()
    except (
        RegiePathError,
        RegieStartupError,
        SettingsError,
        TmuxError,
        OSError,
        ValueError,
    ) as exc:
        print(f"regie: {exc}", file=sys.stderr)
        return 1
    finally:
        if log_handle is not None:
            log_handle.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="regie")
    parser.add_argument("--socket", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--client-id", default="regie-ui")
    commands = parser.add_subparsers(dest="command")
    bridge = commands.add_parser("bridge", help="Run or inspect the persistent tmux provider.")
    bridge_commands = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_commands.add_parser("start", help="Start the bridge and wait for provider readiness.")
    bridge_commands.add_parser(
        "status", help="Show bridge process status without starting Theater."
    )
    bridge_commands.add_parser("stop", help="Stop bridge reporting without touching terminals.")

    worker = commands.add_parser("_bridge-worker", help=argparse.SUPPRESS)
    worker.add_argument("--home", type=Path, required=True)
    worker.add_argument("--socket", type=Path, required=True)
    worker.add_argument("--selector", required=True)
    worker.add_argument("--client-id", required=True)
    worker.add_argument("--token", required=True)
    return parser


def _bridge_command(
    args: argparse.Namespace,
    paths: RegiePaths,
    socket_path: Path,
    manager: BridgeProcessManager,
) -> int:
    if args.bridge_command == "status":
        _print_status(manager.status())
        return 0
    if args.bridge_command == "stop":
        _print_status(manager.stop())
        return 0
    _probe_daemon(paths, socket_path, "regie-bridge-start")
    _print_status(manager.start())
    return 0


def _probe_daemon(paths: RegiePaths, socket_path: Path, client_id: str) -> None:
    paths.ensure_private_runtime()

    async def probe() -> None:
        client, _handshake = await connect_or_start_daemon(
            socket_path=socket_path,
            client_id=client_id,
            required_capabilities=REGIE_REQUIRED_CAPABILITIES,
            log_path=paths.root / "daemon-start.log",
        )
        await client.close()

    asyncio.run(probe())


def _bridge_server_identity(status: BridgeProcessStatus) -> str:
    identity = status.tmux_server_identity
    if not identity:
        raise RegieStartupError("Régie bridge is online without a pinned tmux server identity")
    return identity


def _configure_ui_logging(paths: RegiePaths, server_identity: str) -> LoggingHandle:
    warning: str | None = None
    try:
        live_panes = asyncio.run(tmux_bootstrap.live_pane_ids(server_identity))
        prune_regie_generations(paths.logs_dir, paths.ui_log_path, protected=live_panes)
    except Exception as error:
        warning = f"Régie log pruning skipped: {error}"
    handle = configure_logging(paths.ui_log_path)
    if warning is not None:
        logging.getLogger("regie").warning(warning)
    return handle


def _require_tmux_available() -> None:
    if not tmux_bootstrap.available():
        raise RegieStartupError("tmux is not on PATH; Régie cannot open its control view")


def _regie_command(socket_path: Path, client_id: str) -> tuple[str, ...]:
    """Re-enter this exact installation after tmux has supplied pane identity."""
    return (
        sys.executable,
        "-m",
        "regie",
        "--socket",
        str(socket_path),
        "--client-id",
        client_id,
    )


def _run_app(
    socket_path: Path,
    client_id: str,
    settings,
    expected_server_identity: str,
) -> None:
    """Import Textual only after daemon and bridge readiness were established."""
    from regie.app import RegieApp
    from regie.tmux.presentation import TmuxPresentation

    client = FrontendClient(
        socket_path,
        client_id=client_id,
        required_capabilities=REGIE_REQUIRED_CAPABILITIES,
        request_timeout=10.0,
    )
    app = RegieApp(
        client=client,
        settings=settings,
        presentation=TmuxPresentation(expected_server_identity=expected_server_identity),
    )
    app.run()


async def _worker(args: argparse.Namespace) -> int:
    return await run_bridge_worker(
        paths=RegiePaths(args.home),
        socket_path=args.socket,
        selector=args.selector,
        client_id=args.client_id,
        token=args.token,
    )


def _print_status(status) -> None:
    payload = {key: value for key, value in asdict(status).items() if key != "token"}
    print(json.dumps(payload, sort_keys=True))


__all__ = ["main"]
