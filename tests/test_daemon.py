"""End-to-end over a real unix socket, with tmux stubbed out.

tmux itself is unavailable in the development sandbox, so `new_window` is
replaced by a fake that hands back a pane id. Everything on the Theater side of
that boundary — protocol framing, dispatch, error mapping, identity, lineage —
is exercised for real.
"""

from __future__ import annotations

import asyncio

import pytest

from theater import paths
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.protocol import RemoteError


class FakeTmux:
    """Records what the spawner asked tmux to do."""

    def __init__(self):
        self.windows: list[dict] = []
        self.panes: list[str] = []
        self._next = 0
        #: Panes visible to list_panes, populated by tests that need the sweep.
        self.visible_panes: list = []

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

    @staticmethod
    def available():
        return True


@pytest.fixture
def fake_tmux(monkeypatch):
    fake = FakeTmux()
    import theater.daemon.spawner as spawner_mod

    monkeypatch.setattr(spawner_mod.tmux, "new_window", fake.new_window)
    monkeypatch.setattr(spawner_mod.tmux, "ensure_session", fake.ensure_session)
    monkeypatch.setattr(spawner_mod.tmux, "sessions", fake.sessions)
    monkeypatch.setattr(spawner_mod.tmux, "kill_pane", fake.kill_pane)
    monkeypatch.setattr(spawner_mod.tmux, "list_panes", fake.list_panes)
    monkeypatch.setattr(spawner_mod.tmux, "available", fake.available)
    monkeypatch.setattr(spawner_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    # The server module imports tmux directly, not through spawner.
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod.tmux, "list_panes", fake.list_panes)
    monkeypatch.setattr(server_mod.tmux, "available", fake.available)
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


async def test_ping(client):
    assert (await client.call("ping"))["pong"] is True


async def test_hello_then_list(client):
    me = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    assert me["tier"] == "adopted"
    assert me["addressable"] is True

    rows = await client.call("participants.list")
    assert [r["id"] for r in rows] == [me["id"]]


async def test_hello_is_idempotent_per_pane(client):
    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    assert a["id"] == b["id"]
    assert len(await client.call("participants.list")) == 1


async def test_external_is_not_addressable(client):
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    assert me["tier"] == "external"
    assert me["addressable"] is False


async def test_unknown_method_is_a_structured_error(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("nope")
    assert exc.value.code == "unknown_method"


async def test_missing_parameter_is_a_structured_error(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get")
    assert exc.value.code == "bad_request"


async def test_get_missing_participant(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id="ghost")
    assert exc.value.code == "not_found"


async def test_spawn_creates_an_identified_participant(client, fake_tmux):
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="say hello",
        approval="manual",
        cwd="/tmp",
        tmux_session="main",
    )

    assert record["tier"] == "spawned"
    assert record["tmux_pane"] == "%1"
    assert record["addressable"] is True

    window = fake_tmux.windows[0]
    assert window["session"] == "main"
    assert window["background"] is True
    assert window["command"] == ["vibe", "say hello"]
    # The id must be reachable from inside the pane, and not only via the
    # environment, which the MCP SDK filters.
    assert record["id"] in window["env"]["VIBE_MCP_SERVERS"]


async def test_spawn_writes_a_config_for_claude(client, fake_tmux):
    record = await client.call(
        "spawn", harness="claude", prompt="hi", approval="yolo", cwd="/tmp"
    )
    config = paths.mcp_config_dir() / f"{record['id']}.json"
    assert config.exists()
    assert record["id"] in config.read_text()
    assert "--dangerously-skip-permissions" in fake_tmux.windows[0]["command"]


async def test_spawn_requires_an_approval_mode(client, fake_tmux):
    with pytest.raises(RemoteError) as exc:
        await client.call("spawn", harness="vibe", prompt="hi", cwd="/tmp")
    assert exc.value.code == "bad_request"


async def test_spawn_rejects_an_unknown_harness(client, fake_tmux):
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn", harness="cursor", prompt="hi", approval="manual", cwd="/tmp"
        )
    assert exc.value.code == "bad_request"


async def test_spawned_child_hellos_with_its_given_id(client, fake_tmux):
    child = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )
    # This is what the child's MCP server does on startup: no pane, no cwd it
    # can be trusted on, just the id from argv.
    seen = await client.call("hello", id=child["id"], harness="vibe", cwd="/tmp")

    assert seen["id"] == child["id"]
    assert seen["tier"] == "spawned"
    assert seen["tmux_pane"] == child["tmux_pane"]


async def test_lineage_shows_in_the_tree(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%99", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    tree = await client.call("participants.tree")
    assert len(tree) == 1
    assert tree[0]["id"] == parent["id"]
    assert [c["id"] for c in tree[0]["children"]] == [child["id"]]


async def test_kill_marks_dead_and_hides(client, fake_tmux):
    record = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )
    await client.call("participant.kill", id=record["id"])

    assert await client.call("participants.list") == []
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["addressable"] is False


async def test_the_reaper_notices_a_vanished_pane(daemon, client, fake_tmux, monkeypatch):
    record = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )

    import theater.daemon.server as server_mod

    monkeypatch.setattr(server_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(server_mod.tmux, "run", _fake_list_panes(""))
    await daemon._reap_once()

    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"


async def test_the_reaper_leaves_live_panes_alone(daemon, client, fake_tmux, monkeypatch):
    record = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )

    import theater.daemon.server as server_mod

    monkeypatch.setattr(server_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(server_mod.tmux, "run", _fake_list_panes(record["tmux_pane"]))
    await daemon._reap_once()

    alive = await client.call("participants.get", id=record["id"])
    assert alive["status"] != "dead"


def _fake_list_panes(output: str):
    async def run(*args, **kwargs):
        return output

    return run


async def test_bus_records_the_story(client, fake_tmux):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    kinds = [e["kind"] for e in await client.call("bus.tail")]
    assert "participant.created" in kinds
    assert "participant.pane" in kinds


async def test_pipelined_requests_keep_their_ids(client):
    results = await asyncio.gather(*(client.call("ping") for _ in range(5)))
    assert all(r["pong"] for r in results)


# ---- harness name normalization ------------------------------------------


async def test_hello_normalizes_claude_code_to_claude(client):
    """A misreported harness name must not silently become unobservable."""
    me = await client.call("hello", harness="claude_code", pane="%1", cwd="/tmp")
    assert me["harness"] == "claude"


async def test_hello_normalizes_capitalized_claude(client):
    me = await client.call("hello", harness="Claude", pane="%2", cwd="/tmp")
    assert me["harness"] == "claude"


async def test_hello_normalizes_mistral_vibe(client):
    me = await client.call("hello", harness="mistral-vibe", pane="%3", cwd="/tmp")
    assert me["harness"] == "vibe"


async def test_unknown_harness_name_passes_through(client):
    """A genuinely unknown harness is not rejected — just unobservable."""
    me = await client.call("hello", harness="cursor", pane="%4", cwd="/tmp")
    assert me["harness"] == "cursor"


# ---- adopt ---------------------------------------------------------------


def _make_pane(pane_id="%5", command="vibe", cwd="/tmp/project", session="main", pane_pid=12345):
    from theater.tmux.client import Pane
    return Pane(
        pane_id=pane_id,
        pane_pid=pane_pid,
        cwd=cwd,
        window_id="@1",
        session=session,
        window_name="vibe",
        current_command=command,
    )


async def test_adopt_detects_harness_from_pane_command(client, fake_tmux, monkeypatch):
    """theater adopt maps pane_current_command to a harness name."""
    # When adopt runs, pane_current_command is "theater" (the adopt command
    # itself), not "vibe". The process tree walk finds "vibe" as an ancestor.
    # Simulate that: foreground is "theater", but descendants include "vibe".
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: ["vibe"])

    fake_tmux.visible_panes = [_make_pane("%5", command="theater", cwd="/tmp/proj")]
    record = await client.call("adopt", pane="%5", cwd="/tmp/proj")
    assert record["tier"] == "adopted"
    assert record["harness"] == "vibe"
    assert record["tmux_pane"] == "%5"
    assert record["cwd"] == "/tmp/proj"


async def test_adopt_detects_claude(client, fake_tmux, monkeypatch):
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: ["claude"])

    fake_tmux.visible_panes = [_make_pane("%6", command="theater", cwd="/tmp/cla")]
    record = await client.call("adopt", pane="%6")
    assert record["harness"] == "claude"
    assert record["cwd"] == "/tmp/cla"


async def test_adopt_detects_from_foreground_when_no_descendants(client, fake_tmux, monkeypatch):
    """If the foreground IS the harness (no adopt in flight), detect it directly."""
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: [])

    fake_tmux.visible_panes = [_make_pane("%9", command="vibe", cwd="/tmp/direct")]
    record = await client.call("adopt", pane="%9")
    assert record["harness"] == "vibe"


async def test_adopt_override_harness(client, fake_tmux, monkeypatch):
    """--harness overrides detection when the command is not a known binary."""
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: ["python3"])

    fake_tmux.visible_panes = [_make_pane("%7", command="python3")]
    record = await client.call("adopt", pane="%7", harness="vibe")
    assert record["harness"] == "vibe"


async def test_adopt_unknown_command_yields_unknown_harness(client, fake_tmux, monkeypatch):
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: ["zsh"])

    fake_tmux.visible_panes = [_make_pane("%8", command="zsh")]
    record = await client.call("adopt", pane="%8")
    assert record["harness"] == "unknown"
    assert record["tier"] == "adopted"


async def test_adopt_missing_pane_is_an_error(client, fake_tmux, monkeypatch):
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: [])

    fake_tmux.visible_panes = []
    with pytest.raises(RemoteError) as exc:
        await client.call("adopt", pane="%999")
    assert exc.value.code == "bad_request"


# ---- unmanaged panes -----------------------------------------------------


async def test_unmanaged_finds_harness_panes_with_no_participant(client, fake_tmux, monkeypatch):
    """The sweep walks the process tree, not just the foreground command."""
    import theater.daemon.server as server_mod

    # Pane %10's foreground is "python3" but its tree contains "vibe";
    # pane %12 is just "zsh" with no harness in its tree.
    def fake_descendants(pid):
        return {"12345": ["vibe"], "12346": ["claude"], "12347": ["zsh"]}.get(
            str(pid), []
        )

    monkeypatch.setattr(server_mod, "_descendant_comms", fake_descendants)
    fake_tmux.visible_panes = [
        _make_pane("%10", command="python3", cwd="/tmp/a", pane_pid=12345),
        _make_pane("%11", command="python3", cwd="/tmp/b", pane_pid=12346),
        _make_pane("%12", command="zsh", cwd="/tmp/c", pane_pid=12347),
    ]
    rows = await client.call("participants.unmanaged")
    assert len(rows) == 2
    assert {r["pane"] for r in rows} == {"%10", "%11"}
    harnesses = {r["pane"]: r["harness"] for r in rows}
    assert harnesses["%10"] == "vibe"
    assert harnesses["%11"] == "claude"


async def test_unmanaged_excludes_registered_panes(client, fake_tmux, monkeypatch):
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod, "_descendant_comms", lambda pid: ["vibe"])

    fake_tmux.visible_panes = [
        _make_pane("%20", command="vibe", cwd="/tmp/a"),
        _make_pane("%21", command="claude", cwd="/tmp/b"),
    ]
    await client.call("hello", harness="vibe", pane="%20", cwd="/tmp/a")
    rows = await client.call("participants.unmanaged")
    assert len(rows) == 1
    assert rows[0]["pane"] == "%21"
