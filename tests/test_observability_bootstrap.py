"""Behavior tests for observability phase 2A daemon/client bootstrap."""

from __future__ import annotations

import asyncio
import stat
from argparse import Namespace

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon.lock import DaemonLock, LockHeld
from theater.daemon.server import Daemon

# ---- Daemon lock injection ----------------------------------------------


def test_daemon_accepts_injected_held_lock(theater_home, fake_tmux):
    lock = DaemonLock()
    lock.acquire()
    d = Daemon(harnesses={}, lock=lock)
    assert d._lock is lock
    d._lock.release()


def test_daemon_rejects_unheld_injected_lock(theater_home, fake_tmux):
    lock = DaemonLock()
    with pytest.raises(ValueError, match="must already be held"):
        Daemon(harnesses={}, lock=lock)


def test_daemon_constructor_failure_releases_injected_lock(theater_home, fake_tmux, monkeypatch):
    lock = DaemonLock()
    lock.acquire()
    assert lock.held
    from theater.daemon.observer import Observer

    orig_init = Observer.__init__

    def boom(self, *a, **kw):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(Observer, "__init__", boom)
    with pytest.raises(RuntimeError, match="observer failed"):
        Daemon(harnesses={}, lock=lock)
    assert not lock.held
    monkeypatch.setattr(Observer, "__init__", orig_init)


def test_daemon_constructor_failure_closes_owned_store(theater_home, fake_tmux, monkeypatch):
    """Constructor-created Store is closed on failure; caller's is not."""
    from theater.daemon.observer import Observer
    from theater.daemon.store import Store

    caller_store = Store(paths.db_path())
    lock = DaemonLock()
    lock.acquire()

    def boom(self, *a, **kw):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(Observer, "__init__", boom)
    with pytest.raises(RuntimeError):
        Daemon(store=caller_store, harnesses={}, lock=lock)
    assert caller_store.conn is not None
    caller_store.close()
    assert not lock.held


def test_daemon_gauge_sampler_initialized_none(theater_home, fake_tmux):
    d = Daemon(harnesses={})
    assert d._gauge_sampler is None
    d._lock.release()


# ---- DaemonClient autostart generation files -----------------------------


async def test_autostart_creates_mode_0600_generation(theater_home, monkeypatch):
    """Parent creates a mode-0600 file and passes same fd as stdout+stderr."""
    forked_cmds: list[list[str]] = []
    forked_fds: list[object] = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            forked_cmds.append(cmd)
            forked_fds.append(kw.get("stdout"))

    monkeypatch.setattr("theater.client.subprocess.Popen", FakePopen)
    client = DaemonClient()
    path = await client._start_daemon()
    assert path is not None
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert "--stderr-token" in forked_cmds[0]
    token_idx = forked_cmds[0].index("--stderr-token")
    assert len(forked_cmds[0][token_idx + 1]) == 12
    assert forked_fds[0] is forked_fds[0]  # stdout and stderr are same object


async def test_autostart_cleans_generation_on_popen_failure(theater_home, monkeypatch):
    """If Popen fails, the generation file is cleaned via delete_generation_file."""

    def boom(*a, **kw):
        raise OSError("fork failed")

    monkeypatch.setattr("theater.client.subprocess.Popen", boom)
    client = DaemonClient()
    with pytest.raises(OSError, match="fork failed"):
        await client._start_daemon()
    assert list(paths.home().glob("daemon.*.stderr.log")) == []


async def test_start_daemon_returns_none_when_lock_held(theater_home):
    held = DaemonLock()
    held.acquire()
    try:
        client = DaemonClient()
        assert await client._start_daemon() is None
    finally:
        held.release()


async def test_timeout_names_exact_generation_path(theater_home, monkeypatch):
    from theater import client as client_mod
    from theater.observability.logging import generation_path

    monkeypatch.setattr(client_mod, "START_TIMEOUT", 0.01)
    client = DaemonClient(autostart=False)
    client._stderr_path = generation_path(paths.home(), "abc123def456")
    with pytest.raises(ConnectionError) as exc:
        await client._await_socket()
    assert "abc123def456" in str(exc.value)


async def test_timeout_names_generic_paths_without_token(theater_home, monkeypatch):
    from theater import client as client_mod

    monkeypatch.setattr(client_mod, "START_TIMEOUT", 0.01)
    client = DaemonClient(autostart=False)
    client._stderr_path = None
    with pytest.raises(ConnectionError) as exc:
        await client._await_socket()
    assert "daemon.log" in str(exc.value)


# ---- CLI role lifecycle --------------------------------------------------


def test_cmd_daemon_rejects_invalid_token():
    from theater.cli.commands.process import cmd_daemon

    args = Namespace(command="daemon", log_level="INFO", timing=False, stderr_token="INVALID!")
    assert cmd_daemon(args) == 2


def test_cmd_daemon_accepts_valid_token(monkeypatch):
    from theater.cli.commands.process import cmd_daemon

    captured = {}

    async def fake_run(options=None):
        captured["options"] = options

    monkeypatch.setattr("theater.daemon.server.run", fake_run)
    args = Namespace(command="daemon", log_level="DEBUG", timing=True, stderr_token="abc123def456")
    assert cmd_daemon(args) == 0
    assert captured["options"].stderr_token == "abc123def456"
    assert captured["options"].log_level == "DEBUG"
    assert captured["options"].timing is True


def test_cmd_daemon_deletes_generation_on_lock_held(theater_home, monkeypatch):
    from theater.cli.commands.process import cmd_daemon
    from theater.daemon import server as server_mod
    from theater.observability.logging import generation_path

    token = "deadbeefdead"
    gen_path = generation_path(paths.home(), token)
    gen_path.write_text("")

    async def refuse(options=None):
        raise LockHeld(None)

    monkeypatch.setattr(server_mod, "run", refuse)
    args = Namespace(command="daemon", log_level="INFO", timing=False, stderr_token=token)
    assert cmd_daemon(args) == 1
    assert not gen_path.exists()


def test_cmd_daemon_keeps_generation_on_runtime_error(theater_home, monkeypatch):
    from theater.cli.commands.process import cmd_daemon
    from theater.daemon import server as server_mod
    from theater.observability.logging import generation_path

    token = "cafebabecafe"
    gen_path = generation_path(paths.home(), token)
    gen_path.write_text("")

    async def boom(options=None):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(server_mod, "run", boom)
    args = Namespace(command="daemon", log_level="INFO", timing=False, stderr_token=token)
    assert cmd_daemon(args) == 1
    assert gen_path.exists()


# ---- run() no-arg compatibility -----------------------------------------


def test_run_accepts_none_options(theater_home, fake_tmux, monkeypatch):
    from theater.daemon import server as server_mod

    started = asyncio.Event()

    async def fake_serve(self):
        started.set()
        self.stop()

    monkeypatch.setattr(Daemon, "serve", fake_serve)

    async def fake_aclose(self, **kw):
        self._release_files()

    monkeypatch.setattr(Daemon, "aclose", fake_aclose)
    from theater.observability import runtime as runtime_mod

    class _FakeHandle:
        def shutdown(self):
            pass

    monkeypatch.setattr(runtime_mod, "configure", lambda **kw: _FakeHandle())
    asyncio.run(server_mod.run())
    assert started.is_set()
