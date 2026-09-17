"""Fail-closed isolation for RC10 candidate subprocesses."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe

TMP_ROOT = Path("/tmp")
MAX_UNIX_SOCKET_PATH_BYTES = 100
_OWNER_MARKER = ".rc10-candidate-owner"
_TMUX_ENV = frozenset({"TMUX", "TMUX_PANE", "TMUX_TMPDIR"})
_PYTHON_PATH_ENV = frozenset({"PYTHONHOME", "PYTHONPATH"})
_HARNESS_RUNTIME_ENV = frozenset(
    {
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "OPENCODE_CONFIG",
        "OPENCODE_DB",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_TUI_CONFIG",
        "VIBE_MCP_SERVERS",
    }
)
_FIXED_CANDIDATE_ENV = frozenset({"THEATER_HOME", "TMUX_TMPDIR"})


class CandidateIsolationError(RuntimeError):
    """A candidate sandbox could not establish or prove its isolation."""


class CleanupBlocked(CandidateIsolationError):
    """Cleanup kept a candidate root because deletion would lose reachability."""


@dataclass(frozen=True, slots=True)
class CandidatePaths:
    """Short, unique paths owned by exactly one candidate sandbox."""

    root: Path
    theater_home: Path
    tmux_root: Path

    def socket_paths(self, *, uid: int | None = None) -> tuple[Path, Path]:
        """Return the daemon and tmux socket paths that need to fit on macOS."""
        tmux_uid = os.getuid() if uid is None else uid
        return (
            self.theater_home / "var" / "run" / "daemon.sock",
            self.tmux_root / f"tmux-{tmux_uid}" / "default",
        )


@dataclass(slots=True)
class CandidateProcess:
    """A child started by a sandbox, which makes its identity test-owned."""

    _process: subprocess.Popen[object]

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        self._process.terminate()


def _is_control_runtime_environment(name: str) -> bool:
    """Whether a variable can route a child back into the control installation."""
    return (
        name in _TMUX_ENV
        or name in _PYTHON_PATH_ENV
        or name in _HARNESS_RUNTIME_ENV
        or name.startswith("THEATER_")
    )


def _socket_path_bytes(path: Path) -> int:
    """Count both the displayed and resolved paths; macOS resolves /tmp."""
    return max(len(os.fsencode(path)), len(os.fsencode(path.resolve(strict=False))))


def _sockets_under(root: Path) -> list[Path]:
    try:
        return [path for path in root.rglob("*") if path.is_socket()]
    except OSError as exc:
        raise CleanupBlocked(
            f"could not inspect candidate root {root} for sockets; leaving it reachable: {exc}"
        ) from exc


class CandidateSandbox:
    """Owns one candidate home, tmux root, and any children it starts."""

    def __init__(self, paths: CandidatePaths, owner_token: str) -> None:
        self.paths = paths
        self._owner_token = owner_token
        self._processes: list[CandidateProcess] = []
        self._closed = False

    def environment(
        self,
        base_environment: Mapping[str, str],
        *,
        extra_environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Copy and sanitize one child environment without changing its parent."""
        environment = {
            name: value
            for name, value in base_environment.items()
            if not _is_control_runtime_environment(name)
        }
        if extra_environment is not None:
            fixed = _FIXED_CANDIDATE_ENV | _TMUX_ENV | {"THEATER_ID"}
            conflicting = fixed.intersection(extra_environment)
            if conflicting:
                names = ", ".join(sorted(conflicting))
                raise CandidateIsolationError(
                    f"candidate extras cannot replace isolation variables: {names}"
                )
            environment.update(extra_environment)
        environment["THEATER_HOME"] = str(self.paths.theater_home)
        environment["TMUX_TMPDIR"] = str(self.paths.tmux_root)
        return environment

    def start(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        base_environment: Mapping[str, str],
        cwd: Path,
        extra_environment: Mapping[str, str] | None = None,
    ) -> CandidateProcess:
        """Start an explicit candidate command and record the child it creates."""
        if not argv:
            raise CandidateIsolationError("a candidate command must name an executable")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise CandidateIsolationError(
                f"candidate cwd must be an existing absolute directory: {cwd}"
            )
        command = [os.fspath(item) for item in argv]
        if not Path(command[0]).is_absolute():
            raise CandidateIsolationError(
                f"candidate executable must be an explicit absolute path: {command[0]}"
            )
        process = CandidateProcess(
            subprocess.Popen(
                command,
                cwd=str(cwd),
                env=self.environment(base_environment, extra_environment=extra_environment),
                start_new_session=True,
            )
        )
        self._processes.append(process)
        return process

    def cleanup(self) -> None:
        """Remove this root only after ownership and every child stop state are proven."""
        if self._closed:
            return
        self._assert_ownership()
        running = [process.pid for process in self._processes if process.poll() is None]
        if running:
            pids = ", ".join(str(pid) for pid in running)
            raise CleanupBlocked(
                f"candidate child process(es) {pids} are still running; leaving {self.paths.root} "
                "reachable"
            )

        sockets = _sockets_under(self.paths.root)
        if sockets:
            raise CleanupBlocked(
                f"candidate socket remains at {sockets[0]}; leaving {self.paths.root} reachable "
                "because its server identity and stop state cannot be proven"
            )

        try:
            shutil.rmtree(self.paths.root)
        except OSError as exc:
            raise CleanupBlocked(
                f"could not remove candidate root {self.paths.root}; inspect it before retrying: "
                f"{exc}"
            ) from exc
        self._closed = True

    def _assert_ownership(self) -> None:
        root = self.paths.root
        marker = root / _OWNER_MARKER
        if root.is_symlink() or not root.is_dir():
            raise CleanupBlocked(
                f"candidate root {root} is no longer its owned directory; leaving it alone"
            )
        try:
            mode = marker.lstat().st_mode
            value = marker.read_text(encoding="utf-8")
        except OSError as exc:
            raise CleanupBlocked(
                f"candidate root {root} has no readable ownership marker; leaving it alone: {exc}"
            ) from exc
        if not stat.S_ISREG(mode) or value != f"{self._owner_token}\n":
            raise CleanupBlocked(
                f"candidate root {root} failed its ownership check; leaving it alone"
            )


def create_candidate_sandbox() -> CandidateSandbox:
    """Allocate one short, private `/tmp` root for a candidate subprocess."""
    root = Path(tempfile.mkdtemp(prefix="rc10-", dir=TMP_ROOT))
    paths = CandidatePaths(root=root, theater_home=root / "h", tmux_root=root / "t")
    try:
        paths.theater_home.mkdir(mode=0o700)
        paths.tmux_root.mkdir(mode=0o700)
        marker = root / _OWNER_MARKER
        owner_token = token_urlsafe(24)
        marker.write_text(f"{owner_token}\n", encoding="utf-8")
        marker.chmod(0o600)
    except BaseException:
        shutil.rmtree(root)
        raise
    too_long = [
        f"{path} ({_socket_path_bytes(path)} bytes)"
        for path in paths.socket_paths()
        if _socket_path_bytes(path) > MAX_UNIX_SOCKET_PATH_BYTES
    ]
    if too_long:
        shutil.rmtree(root)
        raise CandidateIsolationError(
            "candidate socket paths exceed the macOS-safe limit: " + ", ".join(too_long)
        )
    return CandidateSandbox(paths, owner_token)
