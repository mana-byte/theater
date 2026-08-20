"""The guard that keeps the suite off the developer's tmux, tested.

On a correct run `reap_private_server` finds nothing and does nothing, so every
interesting branch is one that never executes in practice. That is exactly the
code that rots, and the reason it was pulled out of `conftest.py`: here the
seams can be substituted and each outcome driven directly. No real tmux server
is started — the socket is a real AF_UNIX socket, but nothing is listening on
the other end of it and nothing needs to be.

The seams are substituted, but `_pid_alive` is also tested against real
processes further down. It is the one piece that cannot be judged by
substituting it, because what it has to get right is the behaviour of
`os.kill(pid, 0)` itself.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import tmux_guard
from tmux_guard import GuardError, _pid_alive, reap_private_server


@pytest.fixture
def root():
    # Short and under /tmp: an AF_UNIX path is capped at 104 bytes on macOS and
    # pytest's own tmp_path runs to about 120, the same reason `theater_home`
    # does not use it.
    path = Path(tempfile.mkdtemp(prefix="guardt", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def socketed(root):
    """A root that looks like tmux has been here, without a server behind it."""
    sock_dir = root / "tmux-501"
    sock_dir.mkdir()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_dir / "default"))
    yield root
    sock.close()


class Tmux:
    """A tmux stand-in that answers the guard's queries and records the asking.

    Each query can be told to raise instead, which is the only way to reach the
    recovery paths: a real tmux fails them by being killed mid-question, timing
    out, or disappearing from PATH between two calls.
    """

    def __init__(self, *, pid="4242", panes="%0 zsh", fail=()):
        self.pid = pid
        self.panes = panes
        self.fail = set(fail)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args[0], kwargs))
        if args[0] in self.fail:
            raise RuntimeError(f"tmux said no to {args[0]}")
        if args[0] == "display-message":
            return self.pid
        if args[0] == "list-panes":
            return self.panes
        return ""

    @property
    def commands(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, command: str) -> dict:
        return next(kwargs for name, kwargs in self.calls if name == command)


def _never_called(*args, **kwargs):
    raise AssertionError(f"should not have been called: {args} {kwargs}")


def _dead_pid() -> int:
    """A pid that is certainly gone: a child, exited and reaped."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ---- the ordinary case -------------------------------------------------


def test_an_untouched_root_is_removed_without_consulting_tmux(root):
    """The common path: no socket, so no server, so nothing to ask tmux about."""
    found = reap_private_server(root, available=_never_called, run=_never_called)
    assert found.server_found is False
    assert found.panes == []
    assert not root.exists()


# ---- the tripwire ------------------------------------------------------


def test_panes_found_on_the_private_server_are_reported(socketed):
    found = reap_private_server(
        socketed,
        available=lambda: True,
        run=Tmux(panes="%0 zsh\n%1 python3.12"),
        pid_alive=lambda pid: False,
    )
    assert found.server_found is True
    assert found.panes == ["%0 zsh", "%1 python3.12"]
    assert not socketed.exists()


def test_a_socket_is_the_escape_even_with_nothing_left_to_name(socketed):
    """`exit-empty off` keeps a pane-less server alive, so panes cannot be the verdict."""
    found = reap_private_server(
        socketed,
        available=lambda: True,
        run=Tmux(panes=""),
        pid_alive=lambda pid: False,
    )
    assert found.server_found is True, "the socket alone proves a real server was started"
    assert found.panes == []


def test_blank_inventory_lines_are_not_reported_as_strays(socketed):
    found = reap_private_server(
        socketed,
        available=lambda: True,
        run=Tmux(panes="\n  \n"),
        pid_alive=lambda pid: False,
    )
    assert found.panes == []


# ---- a live server is killed even when the bookkeeping fails -----------
#
# The temptation is to bail out on the first failed query, since the guard can
# no longer report properly. That abandons a running server to keep the error
# handling tidy, which is the same orphan the guard exists to prevent.


def test_a_failed_inventory_still_kills_the_server(socketed):
    """A broken query is not evidence of an empty server, nor a reason to leave it up."""
    tmux = Tmux(fail={"list-panes"})

    with pytest.raises(GuardError, match="could not be inventoried"):
        reap_private_server(socketed, available=lambda: True, run=tmux, pid_alive=lambda pid: False)

    assert "kill-server" in tmux.commands
    assert not socketed.exists(), "the pid proved it died, so the root is only litter"


def test_a_failed_kill_is_named_rather_than_escaping_raw(socketed):
    """`check=False` covers a bad exit code, not a timeout or a missing binary."""
    tmux = Tmux(fail={"kill-server"})

    with pytest.raises(GuardError, match="kill-server could not be run"):
        reap_private_server(socketed, available=lambda: True, run=tmux, pid_alive=lambda pid: False)

    assert not socketed.exists(), "the pid still proved it died, whatever the kill reported"


def test_an_unreadable_pid_does_not_stop_the_inventory(socketed):
    """The panes are still worth collecting; they name the test that escaped."""
    tmux = Tmux(pid="not-a-pid")

    with pytest.raises(GuardError):
        reap_private_server(socketed, available=lambda: True, run=tmux, pid_alive=_never_called)

    assert tmux.commands == ["display-message", "list-panes", "kill-server"]


# ---- every unproven death keeps the root -------------------------------


def test_a_surviving_server_keeps_its_socket_root(socketed):
    """The root is the only handle left on a live server. Deleting it is the bug."""
    clock = iter([0.0, 0.1, 99.0])

    with pytest.raises(GuardError, match="still alive"):
        reap_private_server(
            socketed,
            available=lambda: True,
            run=Tmux(pid="777"),
            pid_alive=lambda pid: True,
            sleep=lambda _: None,
            monotonic=lambda: next(clock),
        )
    assert socketed.exists(), "a server that would not die must stay reachable"


def test_the_surviving_server_error_names_the_root_and_the_pid(socketed):
    clock = iter([0.0, 99.0])

    with pytest.raises(GuardError) as excinfo:
        reap_private_server(
            socketed,
            available=lambda: True,
            run=Tmux(pid="777"),
            pid_alive=lambda pid: True,
            sleep=lambda _: None,
            monotonic=lambda: next(clock),
        )
    message = str(excinfo.value)
    assert "777" in message
    assert str(socketed) in message
    assert "kill-server" in message


def test_a_server_that_dies_while_being_polled_is_waited_for(socketed):
    """Death is not instant. The loop has to outlast a slow shutdown, then stop."""
    remaining = iter([True, True, False])
    naps = []

    found = reap_private_server(
        socketed,
        available=lambda: True,
        run=Tmux(),
        pid_alive=lambda pid: next(remaining),
        sleep=naps.append,
        monotonic=lambda: 0.0,
    )

    assert len(naps) == 2, "one nap per poll that found it still alive"
    assert found.panes == ["%0 zsh"]
    assert not socketed.exists()


def test_an_unreadable_pid_keeps_the_root(socketed):
    """No pid means no proof of death, so the server has to stay reachable."""
    with pytest.raises(GuardError, match="cannot be checked for liveness") as excinfo:
        reap_private_server(
            socketed,
            available=lambda: True,
            run=Tmux(pid="not-a-pid"),
            pid_alive=_never_called,
        )
    assert socketed.exists()
    assert str(socketed) in str(excinfo.value)


def test_a_socket_with_no_tmux_to_ask_is_left_alone(socketed):
    """tmux gone from PATH is not evidence the server died with it."""
    with pytest.raises(GuardError, match="not on PATH"):
        reap_private_server(socketed, available=lambda: False, run=_never_called)
    assert socketed.exists()


# ---- ordering ----------------------------------------------------------


def test_the_server_is_inventoried_before_it_is_killed(socketed):
    """Afterwards there is nothing left to name, so the order is the whole point."""
    tmux = Tmux()

    reap_private_server(socketed, available=lambda: True, run=tmux, pid_alive=lambda pid: False)

    assert tmux.commands == ["display-message", "list-panes", "kill-server"]


def test_the_kill_does_not_check_the_exit_code(socketed):
    """Killing the server drops the connection, so its exit code proves nothing."""
    tmux = Tmux()

    reap_private_server(socketed, available=lambda: True, run=tmux, pid_alive=lambda pid: False)

    assert tmux.kwargs_for("kill-server") == {"check": False}
    assert tmux.kwargs_for("list-panes") == {}, "the queries do want their exit codes checked"


# ---- the liveness probe itself -----------------------------------------
#
# Every test above substitutes `pid_alive`, so the default seam would otherwise
# never run. What it has to get right is the behaviour of `os.kill(pid, 0)`,
# which cannot be established by replacing it.


def test_the_probe_sees_a_living_process():
    assert _pid_alive(os.getpid()) is True


def test_the_probe_sees_a_reaped_process_as_gone():
    assert _pid_alive(_dead_pid()) is False


def test_a_process_we_may_not_signal_still_counts_as_alive(monkeypatch):
    """EPERM is an answer about permission, not about existence.

    Driven rather than provoked. The obvious way to earn a real `PermissionError`
    is to signal pid 1, but that depends on not being root and on pid 1 being
    something we may not touch — neither holds in a container, where the test
    would quietly stop testing this branch instead of failing.

    The `os` name is rebound in the guard's own namespace, not patched on the
    stdlib module: `tmux_guard.os` *is* `os`, so setting `kill` on it would take
    `os.kill` away from the whole process for the duration.
    """

    def refuse(pid, sig):
        raise PermissionError(pid)

    monkeypatch.setattr(tmux_guard, "os", SimpleNamespace(kill=refuse))
    assert _pid_alive(4242) is True
