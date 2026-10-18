"""Harness discovery and optional model policy must match what the daemon accepts."""

from __future__ import annotations

import json

import pytest

from tests.support.plugin_packages import install, write_plugin
from theater.client import DaemonClient
from theater.config import Config
from theater.daemon.server import Daemon
from theater.mcp import tools
from theater.mcp.server import build

#: The adapters Theater ships. Every one must reach an agent.
SHIPPED = {"claude", "codex", "opencode", "pi", "vibe"}


@pytest.fixture
async def daemon(theater_home):
    d = Daemon(
        config=Config(models={"vibe": ["opus-5", "medium-3"]}, reasoning={"claude": ["high"]})
    )
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
    assert all(set(row) == {"name", "approvals"} for row in rows)


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


async def test_optional_models_come_from_daemon_and_filter_by_name(
    daemon, local_dir, all_installed, theater_home, monkeypatch
):
    from theater import harness

    write_plugin(local_dir, binary="acme-cli")
    (local_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    install(local_dir)
    (theater_home / "config.toml").write_text('[models]\nvibe = ["after-boot"]\n')
    monkeypatch.setattr(
        harness.shutil, "which", lambda binary: None if binary == "codex" else f"/bin/{binary}"
    )
    session = tools.Session(participant_id="p1", harness="vibe", client=DaemonClient())
    plain = await tools.harnesses(session)
    detailed = await tools.harnesses(session, include_models=True)
    rows = {row["name"]: row for row in detailed}

    assert set(rows) == {row["name"] for row in plain}
    assert "codex" not in rows and "broken" not in rows
    assert rows["vibe"]["models"] == ["opus-5", "medium-3"]
    assert rows["vibe"]["supported"] is True
    assert rows["claude"]["models"] == []
    assert rows["claude"]["reasoning"] == ["high"]
    assert rows["claude"]["reasoning_supported"] is True
    assert rows["acme"]["binary"] == "acme-cli"
    assert rows["acme"]["supported"] is False
    assert rows["acme"]["models"] == []
    assert all(
        set(row) == {"name", "approvals", "models", "supported", "reasoning", "reasoning_supported"}
        for name, row in rows.items()
        if name != "acme"
    )
    assert await tools.harnesses(session, harness="vibe", include_models=True) == [rows["vibe"]]
    assert await tools.harnesses(session, harness="vibe") == [
        row for row in plain if row["name"] == "vibe"
    ]
    assert await tools.harnesses(session, harness="missing", include_models=True) == []


async def test_spawning_a_listed_harness_is_accepted(daemon, terminal_provider, all_installed):
    """The end of the chain: what list_harnesses offers, spawn_session takes.

    Any name here would have been refused by the old description before the
    daemon ever saw it.
    """
    terminal_provider.install(daemon)
    mcp = build("parent", "vibe")
    await mcp.call_tool("whoami", {})
    session = tools.Session(participant_id="parent", harness="vibe", client=DaemonClient())
    vibe = (await tools.harnesses(session, harness="vibe", include_models=True))[0]

    for name in sorted(SHIPPED):
        child = _payload(
            await mcp.call_tool(
                "spawn_session",
                {
                    "harness": name,
                    "model": vibe["models"][0] if name == "vibe" else None,
                    "prompt": "",
                    "approval": "yolo" if name in ("pi", "opencode") else "manual",
                    # Catalog acceptance only: prove the name is spawnable on
                    # this fake pane path. Codex selects the real native
                    # wiring under the default auto, which would launch a
                    # real app-server backend here, so pin the per-spawn
                    # legacy opt-out for that one iteration.
                    "wiring": "legacy" if name == "codex" else "auto",
                },
            )
        )
        assert child["harness"] == name
        assert child["parent_id"] == "parent"

    refused = await mcp.call_tool(
        "spawn_session",
        {"harness": "vibe", "approval": "manual", "model": "not-on-the-list"},
    )
    message = refused.structured_content["error"]["message"]
    assert "not-on-the-list" in message and "opus-5" in message
