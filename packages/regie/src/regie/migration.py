"""Move a Régie home from its old flat layout into ``var/{state,run,logs}``, once, by itself.

An old bridge still running from the flat layout is stopped first, through the same proven-PID
stop the bridge command uses, so no two bridges ever serve one tmux server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from regie.paths import RegiePathError, RegiePaths
from regie.process import BridgeProcessManager

#: Old runtime files, meaningless once their bridge has stopped.
_OLD_RUNTIME = ("bridge-process.lock", "bridge-start.lock", "bridge.pid", "bridge.status.json")


@dataclass(frozen=True, slots=True)
class _FlatPaths(RegiePaths):
    """Where the flat layout kept a running bridge's facts, to stop it through its own records."""

    @property
    def bridge_process_lock(self) -> Path:
        return self.root / "bridge-process.lock"

    @property
    def bridge_pid_path(self) -> Path:
        return self.root / "bridge.pid"

    @property
    def bridge_status_path(self) -> Path:
        return self.root / "bridge.status.json"


def migrate_flat_layout(paths: RegiePaths, socket_path: Path) -> bool:
    """Move the old flat files into place; returns whether there was anything to move."""
    root, old_ui_logs = paths.root, paths.theater_home / "var" / "logs" / "regie"
    moves = {
        root / "tree-layout.json": paths.tree_layout_path,
        root / "bridge": paths.bridge_state_dir,
        root / "bridge.log": paths.bridge_log_path,
        root / "daemon-start.log": paths.daemon_start_log_path,
    }
    runtime = [root / name for name in _OLD_RUNTIME]
    if not any(path.exists() for path in (*moves, *runtime, old_ui_logs)):
        return False
    old = _FlatPaths(paths.theater_home)
    if BridgeProcessManager(old, socket_path=socket_path).stop().running:
        raise RegiePathError(
            f"a Régie bridge from the previous layout (pid in {old.bridge_pid_path}) did not stop;"
            " stop it, then start Régie again to finish moving its files into var/"
        )
    paths.ensure_private_runtime()
    for source, target in moves.items():
        if source.exists():
            _move(source, target)
    for stale in runtime:
        stale.unlink(missing_ok=True)
    if old_ui_logs.is_dir():  # UI logs leave Theater's tree for Régie's own
        for log in old_ui_logs.iterdir():
            _move(log, paths.ui_logs_dir / log.name)
        old_ui_logs.rmdir()
    return True


def _move(source: Path, target: Path) -> None:
    """Move into place, replacing an empty directory the new layout may already have made."""
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
    if target.exists():
        raise RegiePathError(
            f"cannot move {source} to {target}: both exist; keep one, remove the other"
        )
    source.replace(target)


__all__ = ["migrate_flat_layout"]
