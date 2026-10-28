"""Autouse fixtures that keep tests off the host's Theater home, tmux server and plugin registry.

A test tree gets them by star-importing this module from its root conftest: pytest applies a
conftest's autouse fixtures only below that conftest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.support.tmux_guard import reap_private_server
from theater import paths

__all__ = [
    "clean_registry",
    "no_inherited_pane",
    "opencode_release",
    "private_tmux_socket",
    "shipped_harnesses",
    "theater_home",
]


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux_run_sync(socket: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("tmux", "-S", str(socket), *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "tmux command failed")
    return result.stdout.rstrip("\n")


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


@pytest.fixture(autouse=True)
def opencode_release(monkeypatch, theater_home):
    """Plan the current OpenCode baseline on a data folder of the test's own.

    Environment, since the registry imports its own copy of every plugin package; a real
    opencode a test reaches must never write the host's `~/.local/share/opencode`.
    """
    from theater.harness.builtin.plugins.opencode.runtime_plan import VERSION_POLICY

    monkeypatch.setenv("THEATER_OPENCODE_VERSION", VERSION_POLICY.minimum)
    monkeypatch.setenv("XDG_DATA_HOME", str(theater_home / "xdg-data"))


@pytest.fixture(scope="session", autouse=True)
def private_tmux_socket():
    """Contain any real tmux reached by tests on a disposable private socket."""
    root = Path(tempfile.mkdtemp(prefix="tmuxsock", dir="/tmp"))
    socket = root / f"tmux-{os.getuid()}" / "default"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMUX_TMPDIR", str(root))
        mp.delenv("TMUX", raising=False)
        mp.delenv("TMUX_PANE", raising=False)
        try:
            yield root
        finally:
            found = reap_private_server(
                root,
                available=_tmux_available,
                run=lambda *args, **kwargs: _tmux_run_sync(socket, *args, **kwargs),
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
            "a test unexpectedly started a real tmux server. "
            "It was contained on the session-private socket and has been killed, but the "
            f"test still needs fixing. {detail}\n"
            "Move intentional tmux execution to a marked Régie package test."
        )


@pytest.fixture(autouse=True)
def no_inherited_pane(monkeypatch):
    """Hide the developer's own $TMUX_PANE from every test.

    A real MCP server never sees this variable: the SDK replaces the inherited
    environment with a six-variable allowlist, which is why a participant that
    reports no pane is filed as External. Under pytest the server runs
    in-process, so a suite run from inside tmux inherits a live pane instead:
    every participant is born addressable, and two registered from the same
    shell collapse into one. Three tests in test_server.py assert the real
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
    from theater.harness.loading.importer import PACKAGE_PREFIX
    from theater.mcp_plugins import registry as mcp_registry

    modules = {
        name: module
        for name, module in sys.modules.copy().items()
        if name.startswith(PACKAGE_PREFIX)
    }
    harnesses = dict(harness.HARNESSES)
    aliases = dict(harness._ALIASES)
    registered = dict(harness._PLUGINS)
    broken = list(harness._BROKEN)
    mcp_servers = dict(mcp_registry.MCP_SERVERS)
    mcp_plugins = dict(mcp_registry._PLUGINS)
    mcp_diagnostics = list(mcp_registry._DIAGNOSTICS)
    yield
    # Registry callbacks must keep the module instances they were compiled against.
    for name in tuple(sys.modules):
        if name.startswith(PACKAGE_PREFIX):
            del sys.modules[name]
    sys.modules.update(modules)
    harness.HARNESSES.clear()
    harness.HARNESSES.update(harnesses)
    harness._ALIASES.clear()
    harness._ALIASES.update(aliases)
    harness._PLUGINS.clear()
    harness._PLUGINS.update(registered)
    harness._BROKEN.clear()
    harness._BROKEN.extend(broken)
    mcp_registry.MCP_SERVERS.clear()
    mcp_registry.MCP_SERVERS.update(mcp_servers)
    mcp_registry._PLUGINS.clear()
    mcp_registry._PLUGINS.update(mcp_plugins)
    mcp_registry._DIAGNOSTICS.clear()
    mcp_registry._DIAGNOSTICS.extend(mcp_diagnostics)
