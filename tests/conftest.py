from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon.registry import Registry
from theater.daemon.server import Daemon
from theater.daemon.store import Store


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
        #: Panes visible to list_panes, populated by tests that need the sweep.
        self.visible_panes: list = []
        #: (pane_id, text) pairs delivered through send_keys.
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
        return pane

    async def ensure_session(self, name, *, cwd=None):
        return name

    async def sessions(self):
        return ["main"]

    async def kill_pane(self, pane_id):
        if pane_id in self.panes:
            self.panes.remove(pane_id)

    async def list_panes(self, session=None):
        return list(self.visible_panes)

    async def run(self, *args, check=True):
        """Answer `list-panes -a -F #{pane_id}`, the only raw call the daemon makes."""
        return "\n".join(p.pane_id for p in self.visible_panes)

    async def send_keys(self, pane_id, text, *, enter=True):
        self.sent.append((pane_id, text))

    async def human_present(self, pane_id):
        """No human is ever at a fake pane unless a test says otherwise."""
        return False

    @staticmethod
    def available():
        return True


@pytest.fixture
def fake_tmux(monkeypatch):
    fake = FakeTmux()

    import theater.daemon.methods as methods_mod
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
        "send_keys",
    ):
        monkeypatch.setattr(tmux_client, name, getattr(fake, name))
    monkeypatch.setattr(spawner_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    # methods.py imports human_present by name, so it needs its own patch.
    monkeypatch.setattr(methods_mod, "human_present", fake.human_present)

    return fake


@pytest.fixture
async def daemon(theater_home):
    # No harnesses: these tests exercise the socket, and a real observer would
    # go scanning the developer's own ~/.claude and ~/.vibe for /tmp sessions.
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
