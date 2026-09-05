"""What an agent is told it can spawn.

The registry, the spawner and the `spawn` RPC were all generic long before this
file existed; the harness set was pinned in exactly one place, the sentence in
`spawn_session`'s description that read `"vibe" or "claude"`. That sentence is
the whole of what a model knows about the choice, so codex and opencode were
unspawnable by agents while being spawnable by every other caller — with no
error anywhere, because nothing was ever asked for them.

So the tests here are about the two surfaces that carry the set: the generated
description, and the `list_harnesses` tool that asks the daemon.
"""

from __future__ import annotations

import json

import pytest

from tests.test_harness_plugins import install, write_plugin
from theater.daemon.server import Daemon
from theater.mcp.server import build

#: The adapters Theater ships. Every one must reach an agent.
SHIPPED = {"claude", "codex", "opencode", "pi", "vibe"}


@pytest.fixture
async def daemon(theater_home):
    d = Daemon()
    await d.start()
    yield d
    await d.aclose()


@pytest.fixture
def local_dir(tmp_path):
    """Stand-in for `$THEATER_HOME/plugins`.

    Its twin in test_harness_plugins is a fixture, and fixtures do not cross
    modules; only the two helpers do.
    """
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture
def all_installed(monkeypatch):
    """Every harness binary on PATH.

    `describe` resolves `installed` against the running process, so without
    this the results would depend on which CLIs the developer happens to have —
    passing on one machine and reporting an empty list on the next.
    """
    from theater import harness

    monkeypatch.setattr(harness.shutil, "which", lambda binary: f"/usr/bin/{binary}")


def _payload(result):
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


def _description(mcp_tools, name: str) -> str:
    return next(t.description for t in mcp_tools if t.name == name)


async def test_spawn_names_every_shipped_harness(daemon):
    """The regression. A literal cannot grow, and this is where it showed."""
    text = _description(await build("p1", "vibe").list_tools(), "spawn_session")
    for name in SHIPPED:
        assert name in text, f"{name} is spawnable but not offered"


async def test_spawn_names_a_harness_the_user_added(daemon, local_dir):
    """A plugin someone wrote reaches the schema too, or the extension point
    stops at the humans."""
    write_plugin(local_dir)
    install(local_dir)

    text = _description(await build("p1", "vibe").list_tools(), "spawn_session")
    assert "acme" in text


async def test_list_harnesses_returns_the_shipped_set(daemon, all_installed):
    rows = _payload(await build("p1", "vibe").call_tool("list_harnesses", {}))
    assert {r["name"] for r in rows} >= SHIPPED
    assert all(r["icon"] and r["binary"] for r in rows)
    assert all(set(row) == {"name", "icon", "binary"} for row in rows)


async def test_list_harnesses_omits_what_is_not_installed(daemon, monkeypatch):
    """A harness whose binary is missing is a spawn that fails; do not offer it."""
    from theater import harness

    monkeypatch.setattr(
        harness.shutil,
        "which",
        lambda binary: None if binary == "codex" else f"/usr/bin/{binary}",
    )

    names = {r["name"] for r in _payload(await build("p1", "vibe").call_tool("list_harnesses", {}))}
    assert "codex" not in names
    assert "vibe" in names


async def test_list_harnesses_omits_a_broken_plugin(daemon, local_dir, all_installed):
    """A local plugin that will not import is a human's problem, not an agent's.

    It stays visible in `theater harnesses`, which is where it can be fixed.
    """
    (local_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    install(local_dir)

    names = {r["name"] for r in _payload(await build("p1", "vibe").call_tool("list_harnesses", {}))}
    assert "broken" not in names
    assert names >= SHIPPED


async def test_spawning_a_listed_harness_is_accepted(daemon, fake_tmux, all_installed):
    """The end of the chain: what list_harnesses offers, spawn_session takes.

    Any name here would have been refused by the old description before the
    daemon ever saw it.
    """
    mcp = build("parent", "vibe")
    await mcp.call_tool("whoami", {})

    for name in sorted(SHIPPED):
        child = _payload(
            await mcp.call_tool(
                "spawn_session",
                {
                    "harness": name,
                    "prompt": "",
                    "approval": "yolo" if name == "pi" else "manual",
                },
            )
        )
        assert child["harness"] == name
        assert child["parent_id"] == "parent"
