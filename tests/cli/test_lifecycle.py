"""`theater stop`, `restart` and `daemon` start-up and interrupt handling."""

from __future__ import annotations

from tests.support.cli_fakes import parse
from theater import cli, paths
from theater.cli.commands import maintenance as maintenance_mod


class StoppableDaemon:
    """A daemon that answers shutdown, then takes its socket away."""

    stopped = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self):
        """Reaching the daemon succeeds. Whether it replies is another matter.

        `_shutdown_running_daemon` distinguishes the two: connecting answers
        "is there a daemon", and the reply is allowed to go missing.
        """

    async def call(self, method, **params):
        if method == "shutdown":
            type(self).stopped = True
            paths.socket_path().unlink(missing_ok=True)
            return {"stopping": True}
        return {"pong": True}


class NoDaemon:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        raise FileNotFoundError(paths.socket_path())

    async def __aexit__(self, *exc):
        return False


def test_stop_does_not_start_a_daemon_just_to_stop_it(monkeypatch, capsys):
    monkeypatch.setattr(maintenance_mod, "DaemonClient", NoDaemon)
    assert cli.cmd_stop(parse("stop")) == 0
    assert "no daemon running" in capsys.readouterr().out


def test_stop_shuts_down_a_running_daemon(monkeypatch, capsys):
    StoppableDaemon.stopped = False
    monkeypatch.setattr(maintenance_mod, "DaemonClient", StoppableDaemon)
    assert cli.cmd_stop(parse("stop")) == 0
    assert StoppableDaemon.stopped
    assert "stopping" in capsys.readouterr().out


def test_restart_stops_the_old_daemon_before_starting_one(monkeypatch, capsys):
    StoppableDaemon.stopped = False
    order: list[str] = []
    monkeypatch.setattr(maintenance_mod, "DaemonClient", StoppableDaemon)
    monkeypatch.setattr(maintenance_mod, "call_sync", lambda method, **p: order.append(method))
    assert cli.cmd_restart(parse("restart")) == 0
    assert StoppableDaemon.stopped
    assert order == ["ping"], "the new daemon has to be proved up, not assumed"
    assert "restarted" in capsys.readouterr().out


def test_restart_with_no_daemon_just_starts_one(monkeypatch, capsys):
    started: list[str] = []
    monkeypatch.setattr(maintenance_mod, "DaemonClient", NoDaemon)
    monkeypatch.setattr(maintenance_mod, "call_sync", lambda method, **p: started.append(method))
    assert cli.cmd_restart(parse("restart")) == 0
    assert started == ["ping"]


def test_restart_refuses_to_start_a_second_daemon(monkeypatch, capsys):
    """A socket still held means the old daemon is alive; two would race."""
    paths.socket_path().touch()

    class Deaf(StoppableDaemon):
        async def call(self, method, **params):
            return {"stopping": True}  # says yes, never lets go

    monkeypatch.setattr(maintenance_mod, "DaemonClient", Deaf)
    monkeypatch.setattr(cli, "STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(maintenance_mod, "call_sync", _explode_on_call)
    assert cli.cmd_restart(parse("restart")) == 1
    error = capsys.readouterr().err
    assert "still holding" in error
    assert "after 0.1s" in error


def _explode_on_call(*a, **k):
    raise AssertionError("started a daemon while the old one was still up")


def test_daemon_reports_a_refusal_to_start(monkeypatch, capsys):
    import theater.daemon.server as server_mod

    async def refuse(options=None):
        raise RuntimeError("another daemon holds the lock")

    monkeypatch.setattr(server_mod, "run", refuse)
    assert cli.cmd_daemon(parse("daemon")) == 1
    assert "another daemon" in capsys.readouterr().err


def test_daemon_exits_quietly_on_ctrl_c(monkeypatch):
    import theater.daemon.server as server_mod

    async def interrupted(options=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(server_mod, "run", interrupted)
    assert cli.cmd_daemon(parse("daemon")) == 0
