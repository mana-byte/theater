"""Behavior tests for observability phase 2A daemon/client bootstrap."""

from __future__ import annotations

import asyncio
import stat
from argparse import Namespace

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.config import Config
from theater.config.models import ObservabilitySection
from theater.daemon import lock as lock_mod
from theater.daemon.lock import DaemonLock, LockHeld
from theater.daemon.server import Daemon


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

    def boom(self, *a, **kw):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(Observer, "__init__", boom)
    with pytest.raises(RuntimeError, match="observer failed"):
        Daemon(harnesses={}, lock=lock)
    assert not lock.held


def test_daemon_constructor_failure_releases_lock_when_ensure_home_fails(theater_home, monkeypatch):
    lock = DaemonLock()
    lock.acquire()
    monkeypatch.setattr(paths, "ensure_home", lambda: (_ for _ in ()).throw(OSError("no home")))

    with pytest.raises(OSError, match="no home"):
        Daemon(harnesses={}, lock=lock)

    assert not lock.held


def test_daemon_ensures_home_before_acquiring_own_lock(theater_home, fake_tmux, monkeypatch):
    calls: list[str] = []
    original_ensure = paths.ensure_home
    original_acquire = DaemonLock.acquire

    def ensure_home():
        calls.append("home")
        return original_ensure()

    def acquire(self):
        calls.append("lock")
        return original_acquire(self)

    monkeypatch.setattr(paths, "ensure_home", ensure_home)
    monkeypatch.setattr(DaemonLock, "acquire", acquire)
    daemon = Daemon(harnesses={})
    try:
        assert calls[:2] == ["home", "lock"]
    finally:
        daemon._lock.release()


def test_daemon_constructor_failure_does_not_close_caller_store(
    theater_home, fake_tmux, monkeypatch
):
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
    assert not caller_store.conn.closed
    caller_store.close()
    assert not lock.held


def test_daemon_constructor_failure_closes_owned_store(theater_home, fake_tmux, monkeypatch):
    from theater.daemon import server as server_mod
    from theater.daemon.store import Store

    instances = []

    class SpyStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True
            super().close()

    class FailingObserver:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("observer failed")

    monkeypatch.setattr(server_mod, "Store", SpyStore)
    monkeypatch.setattr(server_mod, "Observer", FailingObserver)

    with pytest.raises(RuntimeError, match="observer failed"):
        Daemon(harnesses={})

    assert len(instances) == 1
    assert instances[0].closed
    assert lock_mod.is_free()


def test_daemon_gauge_sampler_initialized_none(theater_home, fake_tmux):
    d = Daemon(harnesses={})
    assert d._gauge_sampler is None
    d._lock.release()


def test_daemon_composes_active_agent_telemetry(theater_home, fake_tmux, monkeypatch):
    from theater.daemon import server as server_mod

    bridge = object()

    class Sink:
        def record_batch(self, *args):
            pass

        def discard(self, *args):
            pass

    sink = Sink()
    captured = {}

    def create(store, received_bridge, *, enabled):
        captured.update(store=store, bridge=received_bridge, enabled=enabled)
        return sink

    monkeypatch.setattr(server_mod, "metric_bridge", lambda: bridge)
    monkeypatch.setattr(server_mod, "create_agent_telemetry", create)
    daemon = Daemon(
        harnesses={}, config=Config(observability=ObservabilitySection(agent_metrics=True))
    )
    try:
        assert captured == {"store": daemon.store, "bridge": bridge, "enabled": True}
        assert daemon.observer.agent_telemetry is sink
    finally:
        daemon._lock.release()


def test_otlp_disabled_daemon_skips_agent_telemetry_projection(
    theater_home, fake_tmux, monkeypatch
):
    from theater.daemon import server as server_mod

    monkeypatch.setattr(server_mod, "metric_bridge", lambda: None)
    daemon = Daemon(
        harnesses={}, config=Config(observability=ObservabilitySection(otlp_enabled=False))
    )
    try:
        assert daemon.observer.agent_telemetry is None
        assert daemon.observer._reducer._telemetry_fn is None
    finally:
        daemon._lock.release()


async def test_autostart_creates_mode_0600_generation(theater_home, monkeypatch):
    """Parent creates a mode-0600 file and passes same fd as stdout+stderr."""
    forked_cmds: list[list[str]] = []
    forked_fds: list[tuple[object, object]] = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            forked_cmds.append(cmd)
            forked_fds.append((kw.get("stdout"), kw.get("stderr")))

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
    assert forked_fds[0][0] is forked_fds[0][1]


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


@pytest.mark.parametrize("agent_metrics", [True, False])
async def test_run_passes_agent_metric_specs_from_config(
    theater_home, fake_tmux, monkeypatch, agent_metrics
):
    from theater.daemon import server as server_mod
    from theater.daemon.trajectory.telemetry import AGENT_METRIC_SPECS
    from theater.observability import runtime as runtime_mod

    captured = {}

    class Handle:
        def shutdown(self):
            pass

    async def serve(self):
        self.stop()

    async def aclose(self):
        self._release_files()

    def configure(**kwargs):
        captured.update(kwargs)
        return Handle()

    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda: Config(observability=ObservabilitySection(agent_metrics=agent_metrics)),
    )
    monkeypatch.setattr(runtime_mod, "configure", configure)
    monkeypatch.setattr(Daemon, "serve", serve)
    monkeypatch.setattr(Daemon, "aclose", aclose)

    await server_mod.run()

    assert captured["metric_specs"] == (AGENT_METRIC_SPECS if agent_metrics else ())


async def test_run_rejects_invalid_programmatic_token_and_releases_lock(theater_home, fake_tmux):
    from theater.daemon import server as server_mod

    options = server_mod.DaemonRunOptions(stderr_token="INVALID")
    with pytest.raises(ValueError, match="invalid stderr token"):
        await server_mod.run(options)
    assert lock_mod.is_free()


@pytest.mark.parametrize("failure", [RuntimeError("close failed"), asyncio.CancelledError()])
async def test_run_shuts_runtime_when_daemon_aclose_fails(
    theater_home, fake_tmux, monkeypatch, failure
):
    from theater.daemon import server as server_mod
    from theater.observability import runtime as runtime_mod

    class Handle:
        shutdowns = 0

        def shutdown(self):
            self.shutdowns += 1

    handle = Handle()

    async def serve(self):
        return None

    async def fail_close(self):
        raise failure

    monkeypatch.setattr(runtime_mod, "configure", lambda **kwargs: handle)
    monkeypatch.setattr(Daemon, "serve", serve)
    monkeypatch.setattr(Daemon, "aclose", fail_close)

    with pytest.raises(type(failure)):
        await server_mod.run()

    assert handle.shutdowns == 1
    assert lock_mod.is_free()
