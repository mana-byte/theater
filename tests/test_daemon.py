"""End-to-end over a real unix socket, with tmux stubbed out.

tmux itself is unavailable in the development sandbox, so `new_window` is
replaced by a fake that hands back a pane id. Everything on the Theater side of
that boundary — protocol framing, dispatch, error mapping, identity, lineage —
is exercised for real.
"""

from __future__ import annotations

import asyncio

import pytest

from theater import harness as harness_registry
from theater import paths
from theater.harness import HARNESSES
from theater.protocol import RemoteError


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


def allow_models(daemon, **by_harness) -> None:
    """Put models on the daemon's allowlist, as a config file would.

    `Config` is frozen, but the mapping it holds is not, and `models_for` reads
    exactly this dict. Naming the models is not optional in these tests: the
    spawn rail refuses any `--model` a harness has no entry for, so a spawn
    that means to test the wiring has to be permitted first.
    """
    daemon.config.models.update(by_harness)


async def test_spawn_carries_a_model_all_the_way_to_the_pane(
    daemon, client, fake_tmux
):
    """The whole wire, end to end: MCP/CLI param -> SpawnRequest -> plan -> tmux."""
    allow_models(
        daemon, vibe=["mysuperdupermodelname"], claude=["opus-4.1"]
    )
    await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp",
        model="mysuperdupermodelname",
    )
    assert fake_tmux.windows[0]["env"]["VIBE_ACTIVE_MODEL"] == "mysuperdupermodelname"

    await client.call(
        "spawn", harness="claude", prompt="hi", approval="manual", cwd="/tmp",
        model="opus-4.1",
    )
    assert "--model=opus-4.1" in fake_tmux.windows[1]["command"]


async def test_spawn_without_a_model_pins_the_vibe_env_empty(client, fake_tmux):
    """An unset variable would be inherited from the daemon's own environment."""
    await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )
    assert fake_tmux.windows[0]["env"]["VIBE_ACTIVE_MODEL"] == ""


async def test_spawn_refuses_an_impossible_model_before_creating_anything(
    daemon, client, fake_tmux, monkeypatch
):
    """The refusal has to land before step 1, not at the launch plan.

    `plan_launch` runs after the participant and its worktree exist, so a
    harness that cannot take a model would leave both behind — a STARTING
    ghost the régie draws forever — for something knowable up front.

    The model is allowlisted deliberately, so the policy rail passes and the
    *capability* check is what refuses. The two are separate questions — may
    the user spend this, and can this adapter accept it at all — and only one
    of them is under test here.
    """
    from theater.harness import Harness, LaunchPlan

    class LegacyHarness(Harness):
        name = "legacy"
        binary = "legacy"

        def plan_launch(self, *, participant_id, prompt, config_path, approval):
            return LaunchPlan(argv=["legacy"])

    monkeypatch.setitem(HARNESSES, "legacy", LegacyHarness())
    allow_models(daemon, legacy=["whatever"])
    before = len(await client.call("participants.list"))

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn", harness="legacy", prompt="hi", approval="manual", cwd="/tmp",
            model="whatever",
        )
    assert exc.value.code == "bad_request"
    assert len(await client.call("participants.list")) == before
    assert fake_tmux.windows == []


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

    from theater.tmux import client as tmux_client

    monkeypatch.setattr(tmux_client, "available", lambda: True)
    monkeypatch.setattr(tmux_client, "run", _fake_list_panes(""))
    await daemon._reap_once()

    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"


async def test_the_reaper_leaves_live_panes_alone(daemon, client, fake_tmux, monkeypatch):
    record = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
    )

    from theater.tmux import client as tmux_client

    monkeypatch.setattr(tmux_client, "available", lambda: True)
    monkeypatch.setattr(tmux_client, "run", _fake_list_panes(record["tmux_pane"]))
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
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: ["vibe"])

    fake_tmux.visible_panes = [_make_pane("%5", command="theater", cwd="/tmp/proj")]
    record = await client.call("adopt", pane="%5", cwd="/tmp/proj")
    assert record["tier"] == "adopted"
    assert record["harness"] == "vibe"
    assert record["tmux_pane"] == "%5"
    assert record["cwd"] == "/tmp/proj"


async def test_adopt_detects_claude(client, fake_tmux, monkeypatch):
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: ["claude"])

    fake_tmux.visible_panes = [_make_pane("%6", command="theater", cwd="/tmp/cla")]
    record = await client.call("adopt", pane="%6")
    assert record["harness"] == "claude"
    assert record["cwd"] == "/tmp/cla"


async def test_adopt_detects_from_foreground_when_no_descendants(client, fake_tmux, monkeypatch):
    """If the foreground IS the harness (no adopt in flight), detect it directly."""
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: [])

    fake_tmux.visible_panes = [_make_pane("%9", command="vibe", cwd="/tmp/direct")]
    record = await client.call("adopt", pane="%9")
    assert record["harness"] == "vibe"


async def test_adopt_override_harness(client, fake_tmux, monkeypatch):
    """--harness overrides detection when the command is not a known binary."""
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: ["python3"])

    fake_tmux.visible_panes = [_make_pane("%7", command="python3")]
    record = await client.call("adopt", pane="%7", harness="vibe")
    assert record["harness"] == "vibe"


async def test_adopt_unknown_command_yields_unknown_harness(client, fake_tmux, monkeypatch):
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: ["zsh"])

    fake_tmux.visible_panes = [_make_pane("%8", command="zsh")]
    record = await client.call("adopt", pane="%8")
    assert record["harness"] == "unknown"
    assert record["tier"] == "adopted"


async def test_adopt_missing_pane_is_an_error(client, fake_tmux, monkeypatch):
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: [])

    fake_tmux.visible_panes = []
    with pytest.raises(RemoteError) as exc:
        await client.call("adopt", pane="%999")
    assert exc.value.code == "bad_request"


# ---- unmanaged panes -----------------------------------------------------


async def test_unmanaged_finds_harness_panes_with_no_participant(client, fake_tmux, monkeypatch):
    """The sweep walks the process tree, not just the foreground command."""
    import theater.daemon.harness_detect as harness_detect_mod

    # Pane %10's foreground is "python3" but its tree contains "vibe";
    # pane %12 is just "zsh" with no harness in its tree.
    def fake_descendants(pid):
        return {"12345": ["vibe"], "12346": ["claude"], "12347": ["zsh"]}.get(
            str(pid), []
        )

    monkeypatch.setattr(harness_detect_mod, "descendant_comms", fake_descendants)
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
    import theater.daemon.harness_detect as harness_detect_mod
    monkeypatch.setattr(harness_detect_mod, "descendant_comms", lambda pid: ["vibe"])

    fake_tmux.visible_panes = [
        _make_pane("%20", command="vibe", cwd="/tmp/a"),
        _make_pane("%21", command="claude", cwd="/tmp/b"),
    ]
    await client.call("hello", harness="vibe", pane="%20", cwd="/tmp/a")
    rows = await client.call("participants.unmanaged")
    assert len(rows) == 1
    assert rows[0]["pane"] == "%21"


# ---- the harness list --------------------------------------------------


async def test_harnesses_lists_what_the_daemon_can_spawn(client):
    rows = await client.call("harnesses")
    names = {r["name"] for r in rows}
    assert names == set(HARNESSES)
    assert all(r["icon"] and r["binary"] for r in rows)


async def test_harnesses_reports_install_state(client, monkeypatch):
    """The daemon's PATH is the one that matters: it runs the binary."""
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: None)
    rows = await client.call("harnesses")
    assert all(r["installed"] is False and r["path"] is None for r in rows)


async def test_harnesses_is_sorted_so_callers_need_not_re_sort(client):
    rows = await client.call("harnesses")
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)


async def test_harnesses_needs_no_parameters(client):
    """The régie calls it at mount with nothing to say."""
    assert await client.call("harnesses") == await client.call("harnesses", id="x")
