from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from tmux_guard import reap_private_server

from theater import paths
from theater.client import DaemonClient
from theater.daemon.registry import Registry
from theater.daemon.server import Daemon
from theater.daemon.store import Store
from theater.tmux import client as tmux_client
from theater.tmux.client import Pane


@pytest.fixture(autouse=True)
def theater_home(monkeypatch):
    """Relocate all Theater state so tests never touch ~/.theater.

    Not pytest's `tmp_path`: its paths run to ~120 bytes, and sun_path caps a
    unix socket at 104 on macOS. Anything that binds a socket needs a short root.
    """
    root = Path(tempfile.mkdtemp(prefix="thtr-", dir="/tmp"))
    monkeypatch.setenv("THEATER_HOME", str(root))
    paths.ensure_home()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def private_tmux_socket():
    """Keep any tmux the suite reaches on a throwaway server of its own.

    `fake_tmux` is the first line and is now autouse, but it can be stood down
    by a marker and it patches named functions rather than the process, so it
    is a policy, not a boundary. This is the boundary: whatever gets through
    talks to a tmux that belongs to nobody.

    It matters because the alternative is the developer's own server. A caller
    that reaches the real `theater.tmux.client` shells out to a bare `tmux` and
    inherits the environment, and FakeTmux's docstring assumes a sandbox with
    no tmux at all — under that assumption an escape is harmless, because the
    spawn simply fails. Where tmux is installed, and the suite is run from
    inside it, the same escape attaches to the developer's session and really
    launches a harness. Those children outlive the temporary THEATER_HOME that
    owned them, so nothing is left that could reclaim them: this is how the
    machine accumulated several hundred unowned agents.

    `TMUX_TMPDIR` alone does not close it. With `$TMUX` set, tmux talks to the
    server named there and never consults the socket root, so both variables
    have to go; `TMUX_TMPDIR` then catches whatever a test starts fresh.

    Session-scoped because a tmux server is process-wide, and there is no
    reason to pay for one per test. The root is short and under /tmp for the
    sun_path reason given in `theater_home`.

    Isolation is not the whole job: a test that forgets `fake_tmux` still
    *launches* a real harness, it just launches it somewhere disposable. So
    teardown also reports what it found. Anything running on this server is by
    definition a test that reached the real client, and the run fails naming
    it. Containment keeps the machine clean; the tripwire is what gets the test
    fixed.

    The floor has one seam. Fixtures run after conftest import, collection and
    the early hooks, so a module that shells out to tmux at import time would
    still reach the developer's server. Nothing in the suite does that today;
    if it ever does, this has to move into a `pytest_configure` hook.
    """
    root = Path(tempfile.mkdtemp(prefix="tmuxsock", dir="/tmp"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMUX_TMPDIR", str(root))
        mp.delenv("TMUX", raising=False)
        mp.delenv("TMUX_PANE", raising=False)
        try:
            yield root
        finally:
            found = reap_private_server(
                root, available=tmux_client.available, run=tmux_client.run_sync
            )
    if found.server_found:
        # On `server_found`, not on the panes. A socket under this root can only
        # exist because a test started a real server here, and a server can
        # outlive its last pane (`exit-empty off`), so an empty inventory is a
        # missing description of the escape, not the absence of one.
        detail = (
            "It was running:\n  " + "\n  ".join(found.panes)
            if found.panes
            else "By teardown it had no panes left to name."
        )
        raise AssertionError(
            "a test reached the real tmux instead of `fake_tmux` and started a server. "
            "It was contained on the session-private socket and has been killed, but the "
            f"test still needs fixing. {detail}\n"
            "Take `fake_tmux` in the test, or in a fixture it already takes, or mark "
            "it `tmux` if it means to drive a real server."
        )


@pytest.fixture(autouse=True)
def no_inherited_pane(monkeypatch):
    """Hide the developer's own $TMUX_PANE from every test.

    A real MCP server never sees this variable: the SDK replaces the inherited
    environment with a six-variable allowlist, which is why a participant that
    reports no pane is filed as External. Under pytest the server runs
    in-process, so a suite run from inside tmux inherits a live pane instead:
    every participant is born addressable, and two registered from the same
    shell collapse into one. Three tests in test_mcp_server.py assert the real
    behaviour and so failed on a developer's machine and nowhere else.

    Autouse rather than a line in each `daemon` fixture, of which there are
    five: the leak is not the daemon's, it reaches any test that builds an MCP
    tool context.

    `private_tmux_socket` already clears this variable for the whole session,
    and clearing it twice costs nothing. The two are kept apart because they
    answer different questions: that one is about which tmux server a spawn
    reaches, this one about what a participant reports as its own pane. A test
    that sets `TMUX_PANE` deliberately wants it restored when it ends, which
    is a function-scoped concern.
    """
    monkeypatch.delenv("TMUX_PANE", raising=False)


@pytest.fixture(scope="session", autouse=True)
def shipped_harnesses():
    """Register the shipped adapters once, as every real entry point does.

    The registry is empty until `install` runs — see `theater/harness`. In
    production that is the daemon or `main()`; here it has to be someone, or
    every test that expects `claude` and `vibe` to exist would have to say so.
    Session-scoped so it lands before `clean_registry` takes its snapshot.

    `local_dir` points nowhere on purpose: a plugin in the developer's own
    ~/.theater must not change what the suite tests.
    """
    from theater import harness
    from theater.config import Config

    harness.install(Config(), local_dir=Path("/nonexistent/theater-harnesses"))


@pytest.fixture(autouse=True)
def clean_registry(shipped_harnesses):
    """Undo any `harness.install` a test performed.

    The registry is a module-level dict mutated in place — it has to be, since
    other modules hold a reference to that exact object. That makes a plugin
    installed by one test leak into every later test in the process, so
    snapshot and restore rather than trusting each test to clean up after
    itself.
    """
    from theater import harness

    harnesses = dict(harness.HARNESSES)
    aliases = dict(harness._ALIASES)
    registered = dict(harness._PLUGINS)
    broken = list(harness._BROKEN)
    yield
    harness.HARNESSES.clear()
    harness.HARNESSES.update(harnesses)
    harness._ALIASES.clear()
    harness._ALIASES.update(aliases)
    harness._PLUGINS.clear()
    harness._PLUGINS.update(registered)
    harness._BROKEN.clear()
    harness._BROKEN.extend(broken)


@pytest.fixture
def store(theater_home) -> Store:
    s = Store(paths.db_path())
    yield s
    s.close()


@pytest.fixture
def registry(store) -> Registry:
    return Registry(store)


class FakeTmux:
    """Stands in for the whole tmux surface, and records what was asked of it.

    tmux is unavailable in the development sandbox, so `new_window` hands back
    a synthetic pane id instead of creating anything. Everything on the Theater
    side of that boundary — protocol framing, dispatch, identity, lineage, the
    job state machine — is exercised for real.
    """

    def __init__(self) -> None:
        self.windows: list[dict] = []
        self.panes: list[str] = []
        self._next = 0
        #: Panes visible to list_panes. A window created here appears in it,
        #: because a pane that was just made does exist — the delivery gate
        #: reads this list and a fake that forgot its own windows would fail
        #: every send. Tests that need a specific pane still append their own.
        self.visible_panes: list[Pane] = []
        #: (pane_id, text) pairs delivered through deliver_text.
        self.sent: list[tuple[str, str]] = []

    async def new_window(self, *, session, name, cwd, command, env=None, background=True):
        self._next += 1
        pane = f"%{self._next}"
        self.windows.append(
            {
                "pane": pane,
                "session": session,
                "name": name,
                "cwd": cwd,
                "command": command,
                "env": env or {},
                "background": background,
            }
        )
        self.panes.append(pane)
        self.visible_panes.append(
            Pane(
                pane_id=pane,
                # Distinct from the pane id so a test cannot pass by
                # confusing the two.
                pane_pid=10_000 + self._next,
                cwd=cwd,
                window_id=f"@{self._next}",
                session=session,
                window_name=name,
                # tmux reports the program it forked; for a spawned harness
                # that is the harness binary itself.
                current_command=command[0] if command else "sh",
            )
        )
        return pane

    async def ensure_session(self, name, *, cwd=None):
        return name

    async def sessions(self):
        return ["main"]

    async def kill_pane(self, pane_id):
        if pane_id in self.panes:
            self.panes.remove(pane_id)
        self.visible_panes = [p for p in self.visible_panes if p.pane_id != pane_id]

    def add_pane(self, pane_id, *, command="vibe", pid=None, cwd="/tmp"):
        """Declare that a pane exists, and what is running in it."""
        pane = Pane(
            pane_id=pane_id,
            pane_pid=pid if pid is not None else 20_000 + len(self.visible_panes),
            cwd=cwd,
            window_id="@0",
            session="main",
            window_name="w",
            current_command=command,
        )
        self.visible_panes = [p for p in self.visible_panes if p.pane_id != pane_id] + [pane]
        return pane

    def remove_pane(self, pane_id):
        """The pane closed — the CLI exited and took its window with it."""
        self.visible_panes = [p for p in self.visible_panes if p.pane_id != pane_id]

    async def list_panes(self, session=None):
        return list(self.visible_panes)

    async def run(self, *args, check=True):
        """Answer `list-panes -a -F #{pane_id}`, the only raw call the daemon makes."""
        return "\n".join(p.pane_id for p in self.visible_panes)

    async def deliver_text(self, pane_id, text, *, enter=True):
        self.sent.append((pane_id, text))

    async def human_present(self, pane_id):
        """No human is ever at a fake pane unless a test says otherwise."""
        return False

    @staticmethod
    def available():
        return True


@pytest.fixture(autouse=True)
def fake_tmux(request, monkeypatch):
    """Stand in for tmux everywhere, unless a test is marked `tmux`.

    Autouse because the safe default is the fake one. This was opt-in, on the
    reasoning that a sandbox has no tmux and a test that forgot it would fail
    loudly; on a developer's machine tmux is present, so forgetting it instead
    started real servers and real harnesses. Fifty-six tests build a `Spawner`
    directly and patch only `shutil.which`, which fakes binary discovery but
    leaves `ensure_session` live — a whole real tmux server from a test about
    branch names.

    Two markers stand it down, for two different reasons. `tmux` means the test
    drives a real server — `tests/test_tmux_rig.py`, which puts that server on a
    socket root of its own. `unpatched_tmux_client` means the test *is* about the client
    — `tests/test_tmux_client.py`, which asserts argv by patching `run` and
    `run_sync` underneath the functions this fake would otherwise replace.
    """
    if {"tmux", "unpatched_tmux_client"} & set(request.keywords):
        return None
    fake = FakeTmux()
    # The panes most tests take for granted. A test that says
    # `hello(pane="%1")` is describing an agent that is already running
    # somewhere, and since the delivery gate checks that the pane is real,
    # the fake tmux has to agree that it is. Tests about the gate itself
    # reshape this with add_pane / remove_pane.
    for pane_id in ("%1", "%2", "%3"):
        fake.add_pane(pane_id)

    import theater.daemon.spawner as spawner_mod
    from theater.tmux import client as tmux_client

    # The spawner and the daemon both bind `theater.tmux.client`, so patching
    # the module once covers every caller.
    for name in (
        "new_window",
        "ensure_session",
        "sessions",
        "kill_pane",
        "list_panes",
        "available",
        "run",
        "deliver_text",
    ):
        monkeypatch.setattr(tmux_client, name, getattr(fake, name))
    # Rebind the spawner's `shutil`, do NOT reach into the module and edit
    # `which` in place. `spawner_mod.shutil` *is* the one stdlib `shutil`, so
    # patching its attribute pretends every binary on the machine exists, for
    # every caller — including `tmux.available()`, which is itself
    # `shutil.which("tmux") is not None`, and `harness.describe()`. That was
    # survivable while this fixture was opt-in. Autouse, it would quietly
    # rewrite the world for all 1981 tests. The spawner asks `shutil` exactly
    # one question (spawner.py:134), so one answer is the whole seam.
    monkeypatch.setattr(
        spawner_mod, "shutil", SimpleNamespace(which=lambda binary: f"/usr/bin/{binary}")
    )
    # sending.py imports human_present by name, so it needs its own patch.
    from theater.daemon.rpc import sending as sending_mod

    monkeypatch.setattr(sending_mod, "human_present", fake.human_present)

    return fake


@pytest.fixture
async def daemon(theater_home, fake_tmux):
    # No harnesses: these tests exercise the socket, and a real observer would
    # go scanning the developer's own ~/.claude and ~/.vibe for /tmp sessions.
    #
    # `harnesses={}` turns off observation but not spawning: the spawner
    # resolves its adapter from the global plugin registry, so a `spawn` here
    # launches a real CLI. `fake_tmux` is autouse and would apply anyway; it is
    # named here to pin the order, because `start()` must not find a real tmux
    # underneath it.
    d = Daemon(harnesses={})
    await d.start()
    yield d
    await d.aclose()


@pytest.fixture
async def client(daemon):
    c = DaemonClient(autostart=False)
    await c.connect()
    yield c
    await c.aclose()
