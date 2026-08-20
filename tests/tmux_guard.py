"""The floor that keeps the suite's tmux off the developer's own server.

This lives outside `conftest.py` so it can be tested. `tests/test_tmux_guard.py`
drives `reap_private_server` through substituted seams and never starts a real
server, which matters because on a correct run the guard never fires: without a
test of its own it would be free to rot.

Two rules, and they pull in opposite directions.

The first is that the socket root is deleted only once the server is *proven*
gone. The socket inside it is the only remaining handle on whatever is still
running, and deleting it is how a process becomes unreachable — the entire bug
this guard exists to prevent.

The second is that the kill is always attempted. Reachable is not the same as
dead: a stray agent left running burns a machine's resources whether or not
anyone can still find it. So a step that fails is recorded and the sequence
continues, rather than returning early and abandoning a live server out of
tidiness about error handling. Nothing on this server is worth preserving —
by construction it holds only tests that escaped their fake.

The run still fails in every one of those cases. It fails after the kill has
been tried, not instead of it.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: How long to wait for the server to die before giving up on it. Generous
#: because the cost of being wrong is asymmetric: a slow CI box that needed one
#: more second gets a spurious failure, while a premature `rmtree` strands a
#: live server. Polling is on the pid, so a fast shutdown still returns at once
#: and the timeout is a real bound — a `tmux` call is not, since `run_sync`
#: carries its own subprocess timeout on top.
KILL_TIMEOUT = 5.0

_POLL = 0.05


class GuardError(RuntimeError):
    """Teardown could not finish its business with the session-private server.

    Two distinct situations, and the message says which. When the server's
    death could not be *proven* the socket root is left on disk and named, so
    whatever is still running stays reachable. When the server is confirmed
    dead but teardown could not say what it had been running, the root is gone
    and only the reporting failed.
    """


@dataclass(frozen=True)
class ReapResult:
    """What teardown found, kept separate from what it could describe.

    `server_found` is the verdict and `panes` is only ever detail. A private
    socket exists solely because something started a real tmux server against
    the root the suite invented for itself, so its mere presence is the escape;
    the panes just say which test to go and fix.

    They are separate fields because they genuinely come apart. A server with
    no panes is not hypothetical — `exit-empty off` keeps one alive — so a
    guard that treated an empty pane list as "nothing happened" would be
    trusting a tmux default that any config can turn off.
    """

    server_found: bool
    panes: list[str] = field(default_factory=list)


def _pid_alive(pid: int) -> bool:
    """Is this pid still around? Signal 0 asks without delivering anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, but reparented or re-owned. Still alive is the answer.
        return True
    return True


def _sockets_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_socket()]


def reap_private_server(
    root: Path,
    *,
    available: Callable[[], bool],
    run: Callable[..., str],
    pid_alive: Callable[[int], bool] = _pid_alive,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rmtree: Callable[[Path], None] = shutil.rmtree,
    timeout: float = KILL_TIMEOUT,
) -> ReapResult:
    """Tear down the session-private tmux server and report that it existed.

    On an isolated run there is no server, and the result says so. Anything
    else is a test that reached the real client, and the caller turns that into
    a failure — on `server_found`, not on the pane list, which may legitimately
    be empty. The inventory is taken before the kill, since afterwards there is
    nothing left to name.

    Raises `GuardError` for every outcome that leaves the server's fate in
    doubt. The root survives that error whenever the server might still be
    running, and only then.
    """
    sockets = _sockets_under(root)
    if not sockets:
        # The ordinary case: nothing ever spoke to tmux, so there is only an
        # empty directory to remove. Not `ignore_errors` — a root that will not
        # delete is a fact worth surfacing.
        rmtree(root)
        return ReapResult(server_found=False)

    if not available():
        raise GuardError(
            f"a tmux socket exists at {sockets[0]} but tmux is not on PATH, so whether a "
            f"server is still running cannot be established, and there is no way to ask it "
            f"to stop. Leaving {root} in place; inspect it once tmux is available again."
        )

    # Past this point a server may be running and it is ours to destroy, so no
    # failure short-circuits the kill. Each step records what went wrong and
    # the sequence continues; the collected problems are raised at the end,
    # after the server has been dealt with as far as it can be.
    problems: list[str] = []
    pid: int | None = None
    panes: list[str] = []

    try:
        pid_text = run("display-message", "-p", "#{pid}")
    except Exception as exc:
        problems.append(f"its pid could not be read ({exc})")
    else:
        try:
            pid = int(pid_text.strip())
        except ValueError:
            problems.append(
                f"it reported {pid_text!r} as its pid, which cannot be checked for liveness"
            )

    # Before the kill, because afterwards there is nothing left to name.
    try:
        inventory = run("list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}")
    except Exception as exc:
        # An inventory that failed is not an inventory that is empty. Recording
        # it stops a broken query from being read as "all clear".
        problems.append(f"it could not be inventoried ({exc})")
    else:
        panes = [line for line in inventory.splitlines() if line.strip()]

    try:
        # `check=False`: killing the server drops the connection, so tmux's exit
        # code is not a reliable account of what happened. The pid is.
        run("kill-server", check=False)
    except Exception as exc:
        # `check=False` covers a non-zero exit, not a timeout or a vanished
        # binary. Those still have to be caught, or they escape as a bare
        # exception that never names the root it left behind.
        problems.append(f"kill-server could not be run ({exc})")

    if pid is None:
        raise GuardError(
            "the session-private tmux server could not be identified well enough to confirm "
            "it died: " + "; ".join(problems) + f". kill-server was attempted anyway. Leaving "
            f"{root} in place so the server stays reachable — "
            f"`TMUX_TMPDIR={root} tmux kill-server`, then remove the directory."
        )

    deadline = monotonic() + timeout
    while pid_alive(pid):
        if monotonic() >= deadline:
            detail = (" Also: " + "; ".join(problems) + ".") if problems else ""
            raise GuardError(
                f"the session-private tmux server (pid {pid}) was still alive {timeout:g}s "
                f"after kill-server. Its socket root is deliberately left at {root} so the "
                f"server can still be reached — `TMUX_TMPDIR={root} tmux kill-server`, then "
                f"remove the directory.{detail}"
            )
        sleep(_POLL)

    # Proven gone, so the root is now just litter.
    rmtree(root)

    if problems:
        raise GuardError(
            f"the session-private tmux server (pid {pid}) is confirmed dead and {root} has "
            "been removed, but a test had reached the real tmux and teardown could not say "
            "what it was running: " + "; ".join(problems) + ". Find the test that skipped "
            "`fake_tmux`."
        )

    return ReapResult(server_found=True, panes=panes)
