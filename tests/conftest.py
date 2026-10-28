from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from tests.support.isolation import *  # noqa: F403
from tests.support.terminal_provider import FakeTerminalProvider
from theater import paths
from theater.client import DaemonClient
from theater.daemon.registry import Registry
from theater.daemon.server import Daemon
from theater.daemon.store import Store

pytest_plugins = ["tests.support.fixtures"]


@pytest.fixture
def available_harness_binaries(monkeypatch, shipped_harnesses, theater_home) -> None:
    """Simulated launches need executable discovery, not installed agent CLIs."""
    from theater.harness import HARNESSES

    binaries = {harness.binary for harness in HARNESSES.values()}
    real_which = shutil.which

    def which(command, *args, **kwargs):
        if command in binaries:
            return sys.executable
        return real_which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    from theater.harness.contracts import version_probe
    from theater.harness.contracts.version_policy import VersionPolicy

    releases = {}
    for name, module_name, pattern_name in (
        ("claude", "compatibility", "_VERSION"),
        ("codex", "runtime_plan", "_VERSION_TOKEN"),
        ("pi", "runtime_constants", "_VERSION_TOKEN"),
        ("vibe", "version", "_VERSION"),
    ):
        module = importlib.import_module(f"theater.harness.builtin.plugins.{name}.{module_name}")
        policy = VersionPolicy.load(Path(module.__file__).with_name("compatibility.json"))
        releases[getattr(module, pattern_name).pattern] = policy.minimum
    monkeypatch.setattr(
        version_probe, "probe_release", lambda binary, pattern, **kwargs: releases[pattern.pattern]
    )
    monkeypatch.setenv("VIBE_HOME", str(theater_home / "vibe"))


@pytest.fixture
def terminal_provider(monkeypatch, available_harness_binaries) -> FakeTerminalProvider:
    provider = FakeTerminalProvider()
    original_init = Daemon.__init__

    def init(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        provider.install(instance)

    monkeypatch.setattr(Daemon, "__init__", init)
    return provider


@pytest.fixture
def store(theater_home) -> Store:
    s = Store(paths.db_path())
    yield s
    s.close()


@pytest.fixture
def registry(store) -> Registry:
    return Registry(store)


@pytest.fixture
async def daemon(theater_home, request):
    # No harnesses: these tests exercise the socket, and a real observer would
    # go scanning the developer's own ~/.claude and ~/.vibe for /tmp sessions.
    #
    # `harnesses={}` turns off observation. Provider tests install an explicit
    # fake terminal connection before launching anything.
    if "terminal_provider" in request.fixturenames:
        request.getfixturevalue("terminal_provider")
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


def pytest_collection_modifyitems(items):
    """`requires_node` tests skip, not fail, on a machine without node."""
    if shutil.which("node") is not None:
        return
    skip = pytest.mark.skip(reason="node is not installed")
    for item in items:
        if "requires_node" in item.keywords:
            item.add_marker(skip)
