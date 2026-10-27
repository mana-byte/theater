"""One scenario's daemon, Régie bridge and private tmux server: start, restart, stop, evidence.

Everything here is a process or a file below the scenario's root, run in its hermetic environment.
The SDK that drives the stack, and every scenario operation, lives in :mod:`tests.support.live_rig`.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.support.paths import REPO_ROOT

BOOT_SECONDS = 90.0
#: A loaded machine can miss the bridge's own start deadline once; a second miss is inconclusive.
_BOOT_ATTEMPTS = 2
STOP_SECONDS = 20.0
_PANE_CAPTURE_LINES = 200
DAEMON_START_LOG = Path("regie") / "var" / "logs" / "daemon-start.log"


@dataclass(frozen=True)
class Stack:
    """The processes of one scenario, started under ``root`` with the hermetic ``env``."""

    root: Path
    env: Mapping[str, str]

    @property
    def theater_home(self) -> Path:
        return self.root / "th"

    @property
    def socket(self) -> Path:
        return self.theater_home / "var" / "run" / "daemon.sock"

    @property
    def daemon_pid(self) -> int:
        return int((self.theater_home / "var" / "run" / "daemon.pid").read_text().split()[0])

    def boot(self, evidence: Path) -> str | None:
        """Start the bridge, which starts the daemon and the private tmux server; why not, if not.

        A miss is retried once from a clean stack. Each attempt that is cleaned up, and the last
        that fails, leaves its logs under ``evidence`` before they are removed with the sandbox.
        """
        failure: str | None = None
        for attempt in range(1, _BOOT_ATTEMPTS + 1):
            failure = self._start()
            if failure is None:
                return None
            self._copy_logs(evidence / (f"boot-{attempt}" if attempt < _BOOT_ATTEMPTS else "boot"))
            if attempt < _BOOT_ATTEMPTS:
                self.stop()
        return failure

    def stop_daemon(self) -> bool:
        """SIGTERM the daemon and wait; SIGKILL after the deadline. Whether SIGTERM sufficed."""
        return _terminate(self.daemon_pid)

    def start_daemon(self) -> None:
        """Start a detached daemon the way Régie does, its output appended to the same start log.

        The bridge reconnects to it by itself. It is reaped when it exits, as init would a daemon
        the bridge had started, because a zombie still answers ``kill -0``.
        """
        log = self.theater_home / DAEMON_START_LOG
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "theater.cli", "daemon"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                env=dict(self.env),
                cwd=REPO_ROOT,
            )
        threading.Thread(target=process.wait, daemon=True).start()

    def stop(self) -> None:
        """Stop the bridge, the daemon and the private tmux server; every step is attempted."""
        with contextlib.suppress(Exception):
            self._regie("bridge", "stop", timeout=STOP_SECONDS)
        with contextlib.suppress(OSError, ValueError, IndexError):
            _terminate(self.daemon_pid)
        with contextlib.suppress(Exception):
            self.tmux("kill-server")

    def capture(self, target: Path) -> None:
        """Copy the logs and every pane's screen to ``target``; best effort, whatever came up."""
        self._copy_logs(target)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            for pane in self.panes():
                target.mkdir(parents=True, exist_ok=True)
                screen = self.tmux(
                    "capture-pane", "-p", "-t", pane, "-S", f"-{_PANE_CAPTURE_LINES}"
                )
                (target / f"pane-{pane.lstrip('%')}.txt").write_text(screen, encoding="utf-8")

    def tmux(self, *args: str) -> str:
        return self._tmux(*args).stdout

    def tmux_running(self) -> bool:
        try:
            return self._tmux("list-sessions").returncode == 0
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False

    def panes(self) -> list[str]:
        return self.tmux("list-panes", "-a", "-F", "#{pane_id}").split()

    @property
    def tmux_socket(self) -> Path:
        """The private server's socket, always named: tmux falls back to the user's own default
        server when TMUX_TMPDIR cannot be used, and a stack must never be able to reach that one."""
        socket = Path(self.env.get("TMUX_TMPDIR", "")) / f"tmux-{os.getuid()}" / "default"
        if self.root.resolve() not in socket.resolve().parents:
            raise RuntimeError(f"refusing to reach a tmux server outside the stack: {socket}")
        return socket

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", str(self.tmux_socket), *args],
            env=dict(self.env),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def _regie(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "regie", "--socket", str(self.socket), *args],
            env=dict(self.env),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _start(self) -> str | None:
        result = self._regie("bridge", "start", timeout=BOOT_SECONDS)
        if result.returncode != 0:
            return f"the Regie bridge did not start: {result.stderr.strip()[-400:]}"
        return None if self.socket.exists() else "the daemon socket never appeared"

    def _copy_logs(self, target: Path) -> None:
        """Copy the daemon and bridge logs that exist; a stack that never started leaves none."""
        daemon_logs = self.theater_home / "var" / "logs" / "daemon"
        bridge_logs = self.theater_home / "regie" / "var" / "logs"
        # A worker that dies before it configures logging leaves only its raw stderr.
        stderr_dirs = {
            "daemon-stderr": daemon_logs / "stderr",
            "bridge-stderr": bridge_logs / "bridge-stderr",
        }
        sources = [
            (daemon_logs / "daemon.log", target / "daemon.log"),
            (self.theater_home / DAEMON_START_LOG, target / DAEMON_START_LOG.name),
            (bridge_logs / "bridge.log", target / "bridge.log"),
            *(
                (source, target / name / source.name)
                for name, directory in stderr_dirs.items()
                for source in sorted(directory.glob("*.log"))
            ),
        ]
        for source, destination in sources:
            if source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    shutil.copyfile(source, destination)


_EXCERPT_CHARS = 500
_EXCERPT_RECORDS = 3
_LOG_TAIL_BYTES = 64 * 1024
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")
_PROBLEM = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (WARNING|ERROR|CRITICAL)\s+(.*)$")


def log_excerpt(evidence: Path, *, limit: int = _EXCERPT_CHARS) -> str:
    """The last warnings and errors the captured logs hold, short enough to survive CI clipping.

    A report clips a failure to its assertion text, so the cause the daemon or bridge logged is
    lost with the sandbox. Each file gets an equal share of ``limit``, so one noisy log cannot
    starve a later one; an unreadable or absent log adds nothing.
    """
    shown: list[tuple[str, list[str]]] = []
    for log in sorted(evidence.rglob("*.log")):
        try:
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - _LOG_TAIL_BYTES))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        # Raw stderr has no record format: its last lines are the crash.
        records = _raw_records(lines) if "stderr" in log.parent.name else _problem_records(lines)
        if records:
            shown.append((log.relative_to(evidence).as_posix(), records))
    if not shown:
        return ""
    share = limit // len(shown)
    return " ; ".join(f"{name}: {' | '.join(records)}"[:share] for name, records in shown)


def _raw_records(lines: list[str]) -> list[str]:
    return [line.strip()[:200] for line in lines if line.strip()][-_EXCERPT_RECORDS:]


def _problem_records(lines: list[str]) -> list[str]:
    """Distinct warning/error records without their date; a record keeps its last continuation
    line, which is where a logged traceback names the exception."""
    records: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _STAMP.match(line):
            problem = _PROBLEM.match(line)
            current = [f"{problem[1]} {problem[2]}"[:200], ""] if problem else None
            if current is not None:
                records.append(current)
        elif current is not None and line.strip():
            current[1] = line.strip()[:120]
    picked: dict[str, str] = {}
    for header, tail in reversed(records):
        if len(picked) == _EXCERPT_RECORDS:
            break
        picked.setdefault(header, tail)
    return [f"{header} … {tail}" if tail else header for header, tail in reversed(picked.items())]


def _terminate(pid: int) -> bool:
    """SIGTERM ``pid`` and wait for it; SIGKILL after the deadline. Whether SIGTERM was enough."""
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    return False
