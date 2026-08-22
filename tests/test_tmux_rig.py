"""Delivery, asserted against a real tmux server.

Everything else in this suite runs against `FakeTmux`, which is a model of what
we believe tmux does. These tests are the check on that belief: a private tmux
server, a real pane running `rig/pane_app.py`, and assertions on the exact
bytes that arrived at its pty.

Isolation is by `TMUX_TMPDIR`, which moves the socket -- and therefore the
server -- somewhere private. Not `-L`, which would mean threading a socket name
through every call in `theater.tmux.client` and would leave the production path
untested. The directory lives under `/tmp` because a unix socket path is capped
at 104 bytes on macOS and pytest's `tmp_path` alone spends most of that budget.

Marked `tmux`, and skipped when tmux is missing rather than deselected by
default: a delivery suite that silently does not run is worse than no suite. CI
without a tmux binary can still use `-m "not tmux"`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import shlex
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.daemon.methods import _check_pane_identity
from theater.models import StaleTarget
from theater.tmux import bootstrap, client

pytestmark = pytest.mark.tmux

RIG = Path(__file__).parent / "rig" / "pane_app.py"
RIG_READY = "RIG READY"
SESSION = "theater-rig"

#: Bracketed-paste markers, DEC's names for them.
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


# ---- the private server ------------------------------------------------


@pytest.fixture(scope="module")
def tmux_server():
    if not client.available():
        pytest.skip("tmux is not on PATH")
    root = tempfile.mkdtemp(prefix="thr", dir="/tmp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMUX_TMPDIR", root)
        # tmux refuses to attach a client when TERM has no clear capability.
        mp.setenv("TERM", "xterm")
        # Inherited TMUX/TMUX_PANE would point at the developer's own server
        # and make tmux refuse to nest.
        mp.delenv("TMUX", raising=False)
        mp.delenv("TMUX_PANE", raising=False)
        # A detached session is 80x24 by default, which is narrow enough to
        # wrap the text we later read back off the screen.
        client.run_sync("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50")
        try:
            yield SESSION
        finally:
            client.run_sync("kill-server", check=False)
    shutil.rmtree(root, ignore_errors=True)


async def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    """Poll until `predicate()` returns something truthy, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {predicate}")
        await asyncio.sleep(interval)


async def test_regie_bootstrap_reuses_its_real_tmux_window(tmux_server, tmp_path):
    command = ["sleep", "30"]
    first = await bootstrap.ensure_regie_window(str(tmp_path), command=command)
    second = await bootstrap.ensure_regie_window(str(tmp_path), command=command)

    assert first == second
    rows = await client.run(
        "list-windows",
        "-t",
        f"{first[0]}:",
        "-F",
        "#{window_id}\t#{@theater_regie}",
    )
    assert [row for row in rows.splitlines() if row.endswith("\t1")] == [f"{first[1]}\t1"]


async def _session_attached(session: str, expected: str) -> bool:
    attached = await client.run(
        "display-message",
        "-p",
        "-t",
        f"{session}:",
        "#{session_attached}",
    )
    return attached == expected


async def test_detach_current_client_really_detaches_and_keeps_session(tmux_server, tmp_path):
    pane = await client.new_window(
        session=tmux_server,
        name="detach",
        cwd=str(tmp_path),
        command=["sleep", "30"],
    )
    window = await client.display_message("#{window_id}", target=pane)
    await client.run("select-window", "-t", window)

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp("tmux", ["tmux", "attach-session", "-t", tmux_server])

    reaped = False
    try:
        await _wait_for(lambda: _session_attached(tmux_server, "1"))
        await client.run(
            "respawn-pane",
            "-k",
            "-t",
            pane,
            "--",
            sys.executable,
            "-c",
            "from theater.tmux.bootstrap import detach_current_client; detach_current_client()",
        )
        await _wait_for(lambda: _session_attached(tmux_server, "0"))
        assert tmux_server in await client.sessions()
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(master_fd)
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            with contextlib.suppress(ChildProcessError):
                await asyncio.to_thread(os.waitpid, pid, 0)


async def _rig_ready(pane: str) -> bool:
    screen = await client.run("capture-pane", "-p", "-t", pane, check=False)
    return RIG_READY in screen


async def _start_rig(
    session: str,
    tmp_path: Path,
    *,
    under_shell: bool = False,
    modal_on: str | None = None,
) -> tuple[str, Path]:
    """Open a pane running the rig; return its pane id and its byte log.

    `under_shell` wraps the program in `sh -c '...; exec sh'`, which is the
    shape a hand-started CLI has: when it exits the window does not close, a
    shell is left at the prompt, and the next paste would be executed by it.

    One caveat that cost an hour: a *non-interactive* `sh` has job control
    off, so it never puts the rig in its own process group, and
    `#{pane_current_command}` -- which tmux reads from the tty's foreground
    group -- says `bash` for the whole life of the pane. That makes the
    wrapped form a faithful stand-in for a pane whose CLI has died and a
    useless one for a pane whose CLI is alive. An interactive shell does
    create the group, which is why a real `claude` started by hand shows up
    as `claude`. Use the plain form when the test needs a live foreground.
    """
    log = tmp_path / "received.bytes"
    log.touch()
    argv = [sys.executable, str(RIG), "--log", str(log)]
    if modal_on:
        argv += ["--modal-on", modal_on]
    if under_shell:
        inner = " ".join(shlex.quote(a) for a in argv)
        argv = ["sh", "-c", f"{inner}; exec sh"]
    pane = await client.new_window(session=session, name="rig", cwd=str(tmp_path), command=argv)
    # The marker follows DECSET 2004 in one flush, so tmux has consumed the declaration.
    await _wait_for(lambda: _rig_ready(pane))
    return pane, log


async def _received(log: Path, *, at_least: int = 1) -> bytes:
    """The bytes the rig has logged, once there are any."""

    async def read():
        data = log.read_bytes()
        return data if len(data) >= at_least else None

    return await _wait_for(read)


def _paste_body(data: bytes) -> bytes:
    assert PASTE_START in data, f"no paste start marker in {data!r}"
    assert PASTE_END in data, f"no paste end marker in {data!r}"
    return data.split(PASTE_START, 1)[1].split(PASTE_END, 1)[0]


# ---- what a paste looks like on the wire -------------------------------


async def test_a_paste_arrives_wrapped_in_bracket_markers(tmux_server, tmp_path):
    pane, log = await _start_rig(tmux_server, tmp_path)
    await client.deliver_text(pane, "hello world")
    data = await _received(log, at_least=len("hello world") + 12)
    assert _paste_body(data) == b"hello world"


async def test_enter_is_a_separate_event_after_the_paste(tmux_server, tmp_path):
    """Enter must land outside the brackets, or it is text and not a key."""
    pane, log = await _start_rig(tmux_server, tmp_path)
    await client.deliver_text(pane, "submit me")
    data = await _wait_for(lambda: _tail(log, b"\r"))
    assert b"\r" not in _paste_body(data)
    assert data.index(b"\r") > data.index(PASTE_END)


async def test_enter_can_be_withheld(tmux_server, tmp_path):
    """`enter=False` is what a draft is: text in the composer, not submitted."""
    pane, log = await _start_rig(tmux_server, tmp_path)
    await client.deliver_text(pane, "draft only", enter=False)
    await _received(log, at_least=len("draft only") + 12)
    await asyncio.sleep(0.2)  # give a stray CR time to show up
    assert b"\r" not in log.read_bytes()


async def test_a_multiline_prompt_is_one_paste(tmux_server, tmp_path):
    """One pair of markers, so the receiver sees one paste and not two.

    tmux rewrites the embedded newline as CR on the way in -- the same byte
    Enter sends. That is only safe *because* it is bracketed: inside the
    markers an application inserts it, outside it would submit the first line
    and paste the rest into whatever came next.
    """
    pane, log = await _start_rig(tmux_server, tmp_path)
    await client.deliver_text(pane, "line one\nline two")
    data = await _wait_for(lambda: _tail(log, PASTE_END))
    assert data.count(PASTE_START) == 1
    assert data.count(PASTE_END) == 1
    body = _paste_body(data)
    assert b"line one" in body and b"line two" in body


async def test_keybinding_characters_arrive_as_text(tmux_server, tmp_path):
    """The OpenCode incident, reproduced as an assertion.

    `!` opens shell mode in OpenCode and Claude Code, a leading `/` opens the
    command palette almost everywhere, and `send-keys -l` fires both. The
    prompt below is the one that was actually eaten -- `~10` reached zsh and
    came back as "not enough directory stack entries".
    """
    pane, log = await _start_rig(tmux_server, tmp_path)
    prompt = "/done Hey! I'm testing ~10 things & `backticks` $HOME"
    await client.deliver_text(pane, prompt)
    data = await _wait_for(lambda: _tail(log, PASTE_END))
    assert _paste_body(data) == prompt.encode()


async def test_two_panes_do_not_paste_each_others_text(tmux_server, tmp_path):
    """The per-pane buffer name, under the concurrency it was written for."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    one, log_one = await _start_rig(tmux_server, first_dir)
    two, log_two = await _start_rig(tmux_server, second_dir)

    await asyncio.gather(
        client.deliver_text(one, "for the first pane"),
        client.deliver_text(two, "for the second pane"),
    )
    got_one = await _wait_for(lambda: _tail(log_one, PASTE_END))
    got_two = await _wait_for(lambda: _tail(log_two, PASTE_END))
    assert _paste_body(got_one) == b"for the first pane"
    assert _paste_body(got_two) == b"for the second pane"


async def _tail(log: Path, needle: bytes) -> bytes | None:
    # Missing is just "not yet": the pane may not have reached the write.
    try:
        data = log.read_bytes()
    except FileNotFoundError:
        return None
    return data if needle in data else None


# ---- the screen --------------------------------------------------------


async def test_capture_pane_sees_what_the_pane_drew(tmux_server, tmp_path):
    """`capture-pane -p` is the observer's screen oracle; prove it reads back."""
    pane, _ = await _start_rig(tmux_server, tmp_path, modal_on="OPEN SESAME")
    await client.deliver_text(pane, "OPEN SESAME")

    async def screen():
        out = await client.run("capture-pane", "-p", "-t", pane, check=False)
        return out if "== MODAL ==" in out else None

    assert "== MODAL ==" in await _wait_for(screen)


# ---- the pane inventory ------------------------------------------------


async def test_session_scoped_list_panes_sees_the_rig(tmux_server, tmp_path):
    """`list-panes -s -t <session>:`, one of the calls no test had ever run."""
    pane, _ = await _start_rig(tmux_server, tmp_path)
    scoped = await client.list_panes(session=tmux_server)
    assert pane in [p.pane_id for p in scoped]
    everything = await client.list_panes()
    assert pane in [p.pane_id for p in everything]


async def test_kill_pane_removes_it(tmux_server, tmp_path):
    """Also never run against a real server before now."""
    pane, _ = await _start_rig(tmux_server, tmp_path)
    assert await client.pane_exists(pane)
    await client.kill_pane(pane)
    assert await _wait_for(lambda: _gone(pane))


async def _gone(pane: str) -> bool:
    return not await client.pane_exists(pane)


# ---- Phase C's gate, against real panes --------------------------------


def _target(pane: str, *, harness: str, pid: int | None = None):
    return SimpleNamespace(id="rig-participant", tmux_pane=pane, harness=harness, pid=pid)


def _gate_stubs():
    """A registry and a refusal that record instead of touching a daemon.

    The point of these tests is the tmux half of the gate; the registry and the
    bus are already covered against fakes in `test_delivery_gate.py`.
    """
    dead: list[str] = []
    daemon = SimpleNamespace(registry=SimpleNamespace(mark_dead=dead.append))

    def refuse(exc, *, reason):
        raise _Refused(reason, exc)

    return daemon, refuse, dead


class _Refused(Exception):
    def __init__(self, reason, exc):
        super().__init__(reason)
        self.reason = reason
        self.exc = exc
        assert isinstance(exc, StaleTarget), f"expected StaleTarget, got {exc!r}"


async def test_the_gate_allows_a_live_program_it_cannot_identify(tmux_server, tmp_path):
    """ "No harness found" alone must never refuse.

    The rig is not a harness, so the `ps` walk comes back `unknown` -- exactly
    what it would report for an agent whose bash tool is briefly in the
    foreground. The pane's own command is `python3.12`, not a shell, so the
    two halves disagree and the send goes through.
    """
    pane, _ = await _start_rig(tmux_server, tmp_path)
    daemon, refuse, dead = _gate_stubs()
    await _check_pane_identity(daemon, _target(pane, harness="vibe"), refuse)
    assert dead == []


async def test_the_gate_refuses_a_pane_whose_program_exited(tmux_server, tmp_path):
    """Phase C's exit criterion, on a real pane with a real shell in it."""
    pane, _ = await _start_rig(tmux_server, tmp_path, under_shell=True)
    daemon, refuse, dead = _gate_stubs()

    # Ctrl-C quits the rig; `exec sh` takes the pane over.
    await client.run("send-keys", "-t", pane, "C-c")
    await _wait_for(lambda: _foreground_is_shell(pane))

    with pytest.raises(_Refused) as caught:
        await _check_pane_identity(daemon, _target(pane, harness="vibe"), refuse)
    assert caught.value.reason == "harness_gone"
    # A ps-based verdict must not destroy the record.
    assert dead == []


async def _foreground_is_shell(pane: str) -> bool:
    from theater.daemon.harness_detect import is_shell

    info = await client.pane_info(pane)
    return info is not None and is_shell(info.current_command)


async def test_the_gate_refuses_a_pane_tmux_has_closed(tmux_server, tmp_path):
    pane, _ = await _start_rig(tmux_server, tmp_path)
    daemon, refuse, dead = _gate_stubs()
    await client.kill_pane(pane)
    await _wait_for(lambda: _gone(pane))

    with pytest.raises(_Refused) as caught:
        await _check_pane_identity(daemon, _target(pane, harness="vibe"), refuse)
    assert caught.value.reason == "pane_gone"
    # tmux is the witness here, so the demotion is safe.
    assert dead == ["rig-participant"]


async def test_the_gate_refuses_a_pane_whose_pid_changed(tmux_server, tmp_path):
    """`respawn-pane` keeps the pane id and replaces the process behind it."""
    pane, _ = await _start_rig(tmux_server, tmp_path)
    info = await client.pane_info(pane)
    assert info is not None
    daemon, refuse, dead = _gate_stubs()

    # The recorded epoch matches, so this passes.
    await _check_pane_identity(daemon, _target(pane, harness="vibe", pid=info.pane_pid), refuse)

    with pytest.raises(_Refused) as caught:
        await _check_pane_identity(
            daemon, _target(pane, harness="vibe", pid=info.pane_pid + 100_000), refuse
        )
    assert caught.value.reason == "pane_replaced"
    assert dead == ["rig-participant"]


async def test_the_gate_leaves_an_unknown_harness_alone(tmux_server, tmp_path):
    """Adopted participants we could not identify stay addressable."""
    pane, _ = await _start_rig(tmux_server, tmp_path, under_shell=True)
    daemon, refuse, _ = _gate_stubs()
    await client.run("send-keys", "-t", pane, "C-c")
    await _wait_for(lambda: _foreground_is_shell(pane))
    # A shell in the seat, and still no refusal: there is no claim to falsify.
    await _check_pane_identity(daemon, _target(pane, harness="unknown"), refuse)


# ---- the environment we hand a spawned CLI ------------------------------


async def test_new_window_passes_env_through_to_the_program(tmux_server, tmp_path):
    """`-e` is how a spawned participant learns its own id; never run for real.

    The rig writes nothing but bytes it reads, so the check goes through the
    shell: print the variable into the log file the test already watches.
    """
    log = tmp_path / "env.out"
    pane = await client.new_window(
        session=tmux_server,
        name="env",
        cwd=str(tmp_path),
        command=["sh", "-c", f'printf %s "$THEATER_ID" > {shlex.quote(str(log))}; exec sh'],
        env={"THEATER_ID": "deadbeef1234"},
    )
    assert await _wait_for(lambda: _tail(log, b"deadbeef1234"))
    await client.kill_pane(pane)
