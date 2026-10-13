"""Per-run sandbox: fresh dirs, private tmux socket, local mock, offline environment."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .contract import Sandbox
from .mock import MockServer

#: Nothing listens here, so a CLI that honours the proxy variables cannot leave the machine.
_DEAD_PROXY = "http://127.0.0.1:9"
_LOOPBACK = "127.0.0.1,localhost"


@contextmanager
def make_sandbox(harness: str, *, root: Path | None = None) -> Iterator[Sandbox]:
    """Create a sandbox under ``root`` (a fresh temp dir by default) and remove it on exit."""
    owned = root is None
    base = Path(tempfile.mkdtemp(prefix=f"harness-compat-{harness}-")) if root is None else root
    home, config_dir = base / "home", base / "config"
    bin_dir, evidence_dir = base / "bin", base / "evidence"
    cache_dir, data_dir, state_dir, temp_dir = (
        base / "cache",
        base / "data",
        base / "state",
        base / "tmp",
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
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "TERM": "xterm-256color",
        "XDG_CONFIG_HOME": str(config_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_DATA_HOME": str(data_dir),
        "XDG_STATE_HOME": str(state_dir),
        "TMPDIR": str(temp_dir),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PIP_CONFIG_FILE": os.devnull,
        "NPM_CONFIG_CACHE": str(cache_dir / "npm"),
        "ANTHROPIC_API_KEY": "theater-dummy",
        "OPENAI_API_KEY": "theater-dummy",
        "NO_PROXY": _LOOPBACK,
        "no_proxy": _LOOPBACK,
    }
    offline_env = {
        **env,
        "HTTP_PROXY": _DEAD_PROXY,
        "HTTPS_PROXY": _DEAD_PROXY,
        "ALL_PROXY": _DEAD_PROXY,
        "http_proxy": _DEAD_PROXY,
        "https_proxy": _DEAD_PROXY,
        "all_proxy": _DEAD_PROXY,
    }
    try:
        yield Sandbox(
            root=base,
            home=home,
            config_dir=config_dir,
            bin_dir=bin_dir,
            evidence_dir=evidence_dir,
            tmux_socket=base / "tmux.sock",
            mock=mock,
            mock_base_urls=mock.base_urls,
            mock_requests=mock_requests,
            env=env,
            offline_env=offline_env,
        )
    finally:
        mock.stop()
        if owned:
            shutil.rmtree(base, ignore_errors=True)
