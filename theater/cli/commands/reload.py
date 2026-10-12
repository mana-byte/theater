"""`theater reload`: hand hot config changes to the running daemon, which reads its own file."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field

from theater.client import DaemonClient
from theater.config.reload import OUTSIDE_DAEMON
from theater.formatting import tilde
from theater.protocol import RemoteError

_ABSENT = "no daemon running — nothing to reload; `theater restart` starts one that reads the file"
_OLD = "the running daemon predates `theater reload`; `theater restart` applies the file"


@dataclass(frozen=True, slots=True)
class DaemonConfigStatus:
    """The running daemon against the file: match, reload, restart, absent, old or error."""

    state: str
    result: dict = field(default_factory=dict)
    message: str | None = None


def request_reload(*, dry_run: bool) -> dict:
    """One ``config.reload`` call with autostart off: reloading must never start a daemon."""

    async def go():
        async with DaemonClient(autostart=False) as client:
            return await client.call("config.reload", dry_run=dry_run)

    result = asyncio.run(go())
    assert isinstance(result, dict)
    return result


def daemon_config_status() -> DaemonConfigStatus:
    """Compare without applying, for `theater config`; key names only, never values."""
    try:
        result = request_reload(dry_run=True)
    except (FileNotFoundError, ConnectionRefusedError):
        return DaemonConfigStatus("absent")
    except RemoteError as exc:
        if exc.code == "unknown_method":
            return DaemonConfigStatus("old")
        return DaemonConfigStatus("error", message=exc.message)
    except (ConnectionError, OSError) as exc:
        return DaemonConfigStatus("error", message=str(exc) or type(exc).__name__)
    if result["restart_required"]:
        return DaemonConfigStatus("restart", result)
    return DaemonConfigStatus("reload" if result["changed"] else "match", result)


def status_lines(status: DaemonConfigStatus) -> list[str]:
    """The `theater config` footer for one status."""
    if status.state == "absent":
        return ["running daemon: none — the next start reads this file"]
    if status.state == "old":
        return ["running daemon: predates `theater reload` — `theater restart` applies this file"]
    if status.state == "error":
        return [f"running daemon: could not compare — {status.message}"]
    if status.state == "match":
        return ["running daemon: matches this file"]
    changed = ", ".join(status.result["changed"])
    if status.state == "reload":
        return [f"running daemon: `theater reload` applies {changed}"]
    restart = ", ".join(status.result["restart_required"])
    lines = [
        f"running daemon: `theater restart` needed for {restart} "
        "(`theater reload` refuses the whole file while it differs)"
    ]
    if changed:
        lines.append(f"  also pending: {changed}")
    return lines


def _outside_line(keys: list[str]) -> str:
    named = ", ".join(
        f"{key} ({OUTSIDE_DAEMON.get(key, 'read outside the daemon')})" for key in keys
    )
    return f"not daemon settings, already read from the file: {named}"


def _render(result: dict) -> list[str]:
    where = tilde(result["path"])
    changed = ", ".join(result["changed"])
    restart = ", ".join(result["restart_required"])
    dry_run = result.get("dry_run", False)
    if restart:
        verb = "would not reload" if dry_run else "not reloaded"
        lines = [
            f"{verb} — {where} changes settings only `theater restart` applies: {restart}",
            "nothing was applied; the running daemon keeps its current settings",
        ]
        if changed:
            lines.append(f"also pending: {changed}")
    elif changed:
        lines = [f"{'would reload' if dry_run else 'reloaded'} {where}: {changed}"]
    else:
        lines = [f"the running daemon already matches {where}"]
    if result["outside_daemon"]:
        lines.append(_outside_line(result["outside_daemon"]))
    return lines


def cmd_reload(args) -> int:
    """Apply hot config changes without a restart; any restart-only change refuses the whole file.
    Exit 0 when applied, already in sync, or a dry run would apply; otherwise 1.
    """
    try:
        result = request_reload(dry_run=args.dry_run)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"theater: {_ABSENT}", file=sys.stderr)
        return 1
    except RemoteError as exc:
        message = _OLD if exc.code == "unknown_method" else exc.message
        print(f"theater: {message}", file=sys.stderr)
        return 1
    except (ConnectionError, OSError) as exc:
        print(f"theater: the daemon did not answer: {exc or type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for line in _render(result):
            print(line)
    return 1 if result["restart_required"] else 0
