"""What an agent is told it can pass as `model`.

The allowlist landed as a gate before it had a way to be read: `spawn_session`
took a `model`, the daemon refused any name the config did not list, and the
only way to learn the list was a CLI command no agent can run. So an agent
could either omit the model or guess, and a guess is refused with a message
that arrives after the call.

These tests are about the surface that closes that: `list_models`, which asks
the daemon — the process that holds the config the refusal is made from.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.test_harness_plugins import install, write_plugin
from theater.config import Config
from theater.daemon.server import Daemon
from theater.mcp.server import build

#: The adapters Theater ships. Every one takes a model.
SHIPPED = {"claude", "codex", "opencode", "pi", "vibe"}

#: A config the daemon is started with, standing in for one a human wrote.
ALLOWED = {"vibe": ["opus-5", "medium-3"], "claude": ["sonnet-4"]}


@pytest.fixture
async def daemon(theater_home):
    d = Daemon(config=Config(models=dict(ALLOWED)))
    await d.start()
    yield d
    await d.aclose()


@pytest.fixture
def local_dir(tmp_path):
    """Stand-in for `$THEATER_HOME/plugins`."""
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture
def all_installed(monkeypatch):
    """Every harness binary on PATH.

    `describe` resolves `installed` against the running process, so without
    this the rows depend on which CLIs the developer happens to have.
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


async def _rows(mcp) -> dict[str, dict]:
    """`list_models` keyed by harness."""
    rows = _payload(await mcp.call_tool("list_models", {}))
    return {r["harness"]: r for r in rows}


async def test_reports_the_configured_allowlist(daemon, all_installed):
    rows = await _rows(build("p1", "vibe"))

    assert rows["vibe"]["models"] == ["opus-5", "medium-3"]
    assert rows["claude"]["models"] == ["sonnet-4"]


async def test_names_a_harness_with_no_entry_at_all(daemon, all_installed):
    """The absent case is the common one, and the one worth reporting.

    Omitting it would read as "codex takes any model" when it is the opposite:
    codex is the harness that refuses every one.
    """
    rows = await _rows(build("p1", "vibe"))

    assert set(rows) >= SHIPPED
    assert rows["codex"]["models"] == []


async def test_reports_what_the_daemon_holds_not_the_file(daemon, all_installed, theater_home):
    """The reason this is an RPC and not a config read.

    The daemon reads its config once at start-up, so a file edited afterwards
    describes a policy nothing enforces. Reporting the file would send an agent
    to spawn with a model the running daemon refuses.
    """
    (theater_home / "config.toml").write_text(
        '[models]\nvibe = ["written-after-boot"]\n', encoding="utf-8"
    )

    rows = await _rows(build("p1", "vibe"))

    assert rows["vibe"]["models"] == ["opus-5", "medium-3"]


async def test_shipped_adapters_support_model_selection(daemon, all_installed):
    rows = await _rows(build("p1", "vibe"))

    assert all(rows[name]["supported"] for name in SHIPPED)


async def test_an_adapter_that_cannot_take_a_model_says_so(daemon, local_dir, all_installed):
    """Empty list and "cannot" are different, and need different fixes.

    A plugin whose `plan_launch` has no `model` parameter is refused by
    `check_model` however the config reads, so an agent told only that the list
    is empty would suggest a config edit that changes nothing.
    """
    write_plugin(local_dir)
    install(local_dir)

    rows = await _rows(build("p1", "vibe"))

    assert rows["acme"]["supported"] is False
    assert rows["acme"]["models"] == []
    assert rows["vibe"]["supported"] is True


async def test_omits_what_is_not_installed(daemon, monkeypatch):
    """Mirrors `list_harnesses`: an unspawnable harness is not an option."""
    from theater import harness

    monkeypatch.setattr(
        harness.shutil,
        "which",
        lambda binary: None if binary == "claude" else f"/usr/bin/{binary}",
    )

    rows = await _rows(build("p1", "vibe"))

    assert "claude" not in rows
    assert "vibe" in rows


async def test_omits_a_broken_plugin(daemon, local_dir, all_installed):
    """A plugin that will not import is a human's problem, not an agent's."""
    (local_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    install(local_dir)

    rows = await _rows(build("p1", "vibe"))

    assert "broken" not in rows
    assert set(rows) >= SHIPPED


async def test_a_listed_model_is_accepted_by_spawn(daemon, fake_tmux, all_installed):
    """The end of the chain: what list_models reports, spawn_session takes."""
    mcp = build("parent", "vibe")
    rows = await _rows(mcp)

    child = _payload(
        await mcp.call_tool(
            "spawn_session",
            {
                "harness": "vibe",
                "approval": "manual",
                "model": rows["vibe"]["models"][0],
            },
        )
    )

    assert child["harness"] == "vibe"


async def test_a_model_not_listed_is_refused(daemon, fake_tmux, all_installed):
    """The other half: the tool would be decoration if anything else passed.

    The refusal names the allowed set, so an agent that guessed anyway is told
    what it should have called `list_models` for.
    """
    mcp = build("parent", "vibe")

    with pytest.raises(ToolError) as exc:
        await mcp.call_tool(
            "spawn_session",
            {"harness": "vibe", "approval": "manual", "model": "not-on-the-list"},
        )

    assert "not-on-the-list" in str(exc.value)
    assert "opus-5" in str(exc.value)
