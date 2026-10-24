"""Per-run sandbox: fresh dirs, private tmux socket, local mock, offline environment."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .cleanup import stop_sandbox_processes
from .contract import Sandbox
from .mock import MockServer
from .process import SANDBOX_ENV, new_token

#: Nothing listens here, so a CLI that honours the proxy variables cannot leave the machine.
_DEAD_PROXY = "http://127.0.0.1:9"
_LOOPBACK = "127.0.0.1,localhost"


def sandbox_environment(
    *,
    token: str,
    home: Path,
    config: Path,
    cache: Path,
    data: Path,
    state: Path,
    temp: Path,
    path: str,
    offline: bool,
) -> dict[str, str]:
    """The one environment policy of everything a sandbox starts, here and in the live suite's rig.

    Private homes and caches, the sandbox token, user git, pip and npm configuration discarded and,
    when ``offline``, every proxy variable (either case) aimed at a dead port: a CLI that honours
    one cannot leave the machine, and loopback stays reachable for the mock.
    """
    env = {
        SANDBOX_ENV: token,
        "HOME": str(home),
        "PATH": path,
        "TERM": "xterm-256color",
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "XDG_DATA_HOME": str(data),
        "XDG_STATE_HOME": str(state),
        "TMPDIR": str(temp),
        "TMUX_TMPDIR": str(temp),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PIP_CONFIG_FILE": os.devnull,
        "NPM_CONFIG_CACHE": str(cache / "npm"),
        "NO_PROXY": _LOOPBACK,
        "no_proxy": _LOOPBACK,
    }
    if offline:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[name] = env[name.lower()] = _DEAD_PROXY
    return env


def _short_temp_dir() -> Path | None:
    temp = Path("/tmp")
    return temp.resolve() if temp.is_dir() else None


def _new_temp_dir() -> Path:
    if short_temp := _short_temp_dir():
        try:
            return Path(tempfile.mkdtemp(prefix="", dir=short_temp)).resolve()
        except OSError:
            pass
    return Path(tempfile.mkdtemp(prefix="")).resolve()


def _unlock(directory: Path) -> None:
    """Make every directory below ``directory`` listable and writable so it can be deleted."""
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                _unlock(child)


def _remove_tree(path: Path) -> None:
    """Delete ``path`` despite read-only or unreadable directories; report what stays behind."""
    if not path.exists():
        return
    _unlock(path)
    stuck: list[str] = []
    shutil.rmtree(path, onexc=lambda _function, target, _error: stuck.append(str(target)))
    if stuck or path.exists():
        sys.stderr.write(f"harness-compat: sandbox cleanup left {path} ({len(stuck)} paths)\n")


@contextmanager
def make_sandbox(harness: str, *, root: Path | None = None) -> Iterator[Sandbox]:
    """Create a sandbox under ``root`` (a fresh temp dir by default) and remove it on exit."""
    owned = root is None
    base = _new_temp_dir() if root is None else root.resolve()
    socket_root: Path | None = None
    tmux_socket = base / "tmux.sock"
    try:
        if root is not None and len(os.fsencode(tmux_socket)) >= 100:
            socket_root = _new_temp_dir()
            tmux_socket = socket_root / "tmux.sock"
        if len(os.fsencode(tmux_socket)) >= 100:
            raise RuntimeError(f"{harness} tmux AF_UNIX socket path is too long: {tmux_socket}")
        yield from _sandbox(base, tmux_socket)
    finally:
        if socket_root is not None:
            _remove_tree(socket_root)
        if owned:
            _remove_tree(base)


def _sandbox(base: Path, tmux_socket: Path) -> Iterator[Sandbox]:
    home, config_dir = base / "h", base / "c"
    bin_dir, evidence_dir = base / "b", base / "e"
    cache_dir, data_dir, state_dir, temp_dir = (
        base / "k",
        base / "d",
        base / "s",
        base / "t",
    )
    for directory in (
        home,
        config_dir,
        bin_dir,
        evidence_dir,
        cache_dir,
        data_dir,
        state_dir,
        temp_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    mock_requests = evidence_dir / "mock_requests.jsonl"
    mock = MockServer(mock_requests)
    mock.start()
    token = new_token()

    def environment(*, offline: bool) -> dict[str, str]:
        return sandbox_environment(
            token=token,
            home=home,
            config=config_dir,
            cache=cache_dir,
            data=data_dir,
            state=state_dir,
            temp=temp_dir,
            path=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            offline=offline,
        )

    env, offline_env = environment(offline=False), environment(offline=True)
    try:
        yield Sandbox(
            root=base,
            home=home,
            config_dir=config_dir,
            bin_dir=bin_dir,
            evidence_dir=evidence_dir,
            tmux_socket=tmux_socket,
            mock=mock,
            mock_base_urls=mock.base_urls,
            mock_requests=mock_requests,
            env=env,
            offline_env=offline_env,
        )
    finally:
        stop_sandbox_processes(base, token, tmux_socket)
        mock.stop()
