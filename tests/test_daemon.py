"""End-to-end over a real unix socket, with tmux stubbed out.

tmux itself is unavailable in the development sandbox, so `new_window` is
replaced by a fake that hands back a pane id. Everything on the Theater side of
that boundary — protocol framing, dispatch, error mapping, identity, lineage —
is exercised for real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from theater import harness as harness_registry
from theater import paths
from theater.daemon import methods
from theater.harness import HARNESSES
from theater.protocol import RemoteError

_JSON_SCHEMA_PREFIX = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)


def _json_prompt(schema: str, prompt: str) -> str:
    return f"{_JSON_SCHEMA_PREFIX.format(schema=schema)}\n\n{prompt}"


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


async def test_spawn_response_format_augments_and_persists_prompt(client, fake_tmux):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    serialized = '{"properties":{"answer":{"type":"string"}},"type":"object"}'
    expected = _json_prompt(serialized, "say hello")

    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="say hello",
        approval="manual",
        cwd="/tmp",
        response_format=schema,
    )

    assert fake_tmux.windows[0]["command"] == ["vibe", expected]
    assert expected.count("Return your final answer as a single bare JSON value") == 1
    job = await client.call("jobs.status", handle=record["handle"])
    assert job["prompt"] == expected
    assert job["response_format"] == serialized
    assert job["structured_result"] is None
    assert job["structured_status"] is None


async def test_promptless_spawn_with_empty_response_format_stays_running(client, fake_tmux):
    expected = _json_prompt("{}", "")
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        response_format={},
    )

    assert fake_tmux.windows[0]["command"] == ["vibe", expected]
    job = await client.call("jobs.status", handle=record["handle"])
    assert job["state"] == "running"
    assert job["prompt"] == expected
    assert job["response_format"] == "{}"


async def test_spawn_response_format_rejects_non_object_before_side_effects(client, fake_tmux):
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn",
            harness="vibe",
            prompt="say hello",
            approval="manual",
            cwd="/tmp",
            response_format=[],
        )

    assert exc.value.code == "bad_request"
    assert "response_format must be a JSON object or null" in str(exc.value)
    assert fake_tmux.windows == []


async def test_spawn_response_format_refuses_resume_that_drops_prompt_before_side_effects(
    client, fake_tmux, monkeypatch
):
    from theater.harness import Harness, LaunchPlan

    class DropsPromptHarness(Harness):
        name = "drops-prompt-rpc"
        binary = "drops-prompt-rpc"
        resume_takes_prompt = False

        def plan_launch(
            self,
            *,
            participant_id,
            prompt,
            config_path,
            approval,
            resume=None,
        ):
            return LaunchPlan(argv=["drops-prompt-rpc"])

    monkeypatch.setitem(HARNESSES, "drops-prompt-rpc", DropsPromptHarness())

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn",
            harness="drops-prompt-rpc",
            prompt="",
            approval="manual",
            cwd="/tmp",
            resume="sess-abc",
            response_format={},
        )

    assert exc.value.code == "bad_request"
    assert "response_format" in str(exc.value)
    assert await client.call("participants.list") == []
    assert fake_tmux.windows == []


async def test_a_freshly_spawned_participant_is_idle_before_hello(client, fake_tmux):
    """A spawned participant is IDLE from the moment it is created.

    Before the STARTING status was removed, a participant that never said
    hello could be pinned at STARTING forever. Starting at IDLE deletes that
    failure class entirely.
    """
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    assert record["status"] == "idle"

    fetched = await client.call("participants.get", id=record["id"])
    assert fetched["status"] == "idle"


async def test_spawn_writes_a_config_for_claude(client, fake_tmux):
    record = await client.call("spawn", harness="claude", prompt="hi", approval="yolo", cwd="/tmp")
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
        await client.call("spawn", harness="cursor", prompt="hi", approval="manual", cwd="/tmp")
    assert exc.value.code == "bad_request"


def allow_models(daemon, **by_harness) -> None:
    """Put models on the daemon's allowlist, as a config file would.

    `Config` is frozen, but the mapping it holds is not, and `models_for` reads
    exactly this dict. Naming the models is not optional in these tests: the
    spawn rail refuses any `--model` a harness has no entry for, so a spawn
    that means to test the wiring has to be permitted first.
    """
    daemon.config.models.update(by_harness)


async def test_spawn_carries_a_model_all_the_way_to_the_pane(daemon, client, fake_tmux):
    """The whole wire, end to end: MCP/CLI param -> SpawnRequest -> plan -> tmux."""
    allow_models(daemon, vibe=["mysuperdupermodelname"], claude=["opus-4.1"])
    await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        model="mysuperdupermodelname",
    )
    assert fake_tmux.windows[0]["env"]["VIBE_ACTIVE_MODEL"] == "mysuperdupermodelname"

    await client.call(
        "spawn",
        harness="claude",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        model="opus-4.1",
    )
    assert "--model=opus-4.1" in fake_tmux.windows[1]["command"]


async def test_spawn_without_a_model_pins_the_vibe_env_empty(client, fake_tmux):
    """An unset variable would be inherited from the daemon's own environment."""
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    assert fake_tmux.windows[0]["env"]["VIBE_ACTIVE_MODEL"] == ""


async def test_spawn_refuses_an_impossible_model_before_creating_anything(
    daemon, client, fake_tmux, monkeypatch
):
    """The refusal has to land before step 1, not at the launch plan.

    `plan_launch` runs after the participant and its worktree exist, so a
    harness that cannot take a model would leave both behind — a ghost
    the régie draws forever — for something knowable up front.

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
            "spawn",
            harness="legacy",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            model="whatever",
        )
    assert exc.value.code == "bad_request"
    assert len(await client.call("participants.list")) == before
    assert fake_tmux.windows == []


async def test_spawned_child_hellos_with_its_given_id(client, fake_tmux):
    child = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
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
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    assert await client.call("participants.list") == []
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["addressable"] is False


async def test_kill_from_a_caller_who_is_the_parent_succeeds(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    result = await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    assert result == {"id": child["id"], "killed": True}
    dead = await client.call("participants.get", id=child["id"])
    assert dead["status"] == "dead"


async def test_kill_refuses_a_target_that_is_not_the_callers_child(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    stranger = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
    )
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=stranger["id"], caller_id=parent["id"])
    assert exc.value.code == "not_your_child"
    alive = await client.call("participants.get", id=stranger["id"])
    assert alive["status"] != "dead"


async def test_kill_refuses_self_kill(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=parent["id"], caller_id=parent["id"])
    assert exc.value.code == "no_self_kill"
    alive = await client.call("participants.get", id=parent["id"])
    assert alive["status"] != "dead"


async def test_kill_on_an_already_dead_child_is_a_no_op(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    result = await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    assert result == {"id": child["id"], "killed": False, "reason": "already_dead"}


async def test_kill_without_caller_id_is_unrestricted(client, fake_tmux):
    """The CLI and the régie send no caller_id; a human may kill anything."""
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    stranger = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
    )
    result = await client.call("participant.kill", id=stranger["id"])
    assert result == {"id": stranger["id"], "killed": True}
    result = await client.call("participant.kill", id=parent["id"])
    assert result == {"id": parent["id"], "killed": True}


async def test_kill_leaves_record_alive_when_pane_survives(client, fake_tmux, monkeypatch):
    """A pane that survives kill-pane must not be marked dead.

    The whole point of the polling: a live pane with a dead record is the ghost
    row the unmanaged sweep rediscovers. Here kill_pane is a no-op, so the pane
    is still in visible_panes and pane_info finds it on every poll. The call
    must fail, and the record must stay alive.
    """
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")

    # Override kill_pane so it does not remove the pane, simulating a pane
    # that tmux failed to reap.
    async def noop_kill(pane_id):
        pass

    from theater.tmux import client as tmux_client

    monkeypatch.setattr(tmux_client, "kill_pane", noop_kill)

    # Patch asyncio.sleep so the test does not actually wait through the poll
    # interval. The bounded retry runs its full course; only the sleep is
    # elided.
    async def noop_sleep(_):
        return

    monkeypatch.setattr(asyncio, "sleep", noop_sleep)

    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=record["id"])
    assert exc.value.code == "error"
    alive = await client.call("participants.get", id=record["id"])
    assert alive["status"] != "dead"


async def test_kill_succeeds_when_pane_disappears_on_a_later_poll(client, fake_tmux, monkeypatch):
    """A pane that only vanishes after the second poll still succeeds.

    tmux reaps panes asynchronously, so the first pane_info may still see the
    pane. The poll loop must keep trying until the pane is gone rather than
    giving up after one check.
    """
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")

    from theater.tmux import client as tmux_client

    pane_id = record["tmux_pane"]
    poll_count = 0

    async def noop_kill(pid):
        pass

    async def pane_info_delayed(pid):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            # First poll: tmux has not reaped the pane yet.
            return _make_pane(pane_id)
        # Second poll: pane is gone.
        return None

    monkeypatch.setattr(tmux_client, "kill_pane", noop_kill)
    monkeypatch.setattr(tmux_client, "pane_info", pane_info_delayed)

    async def noop_sleep(_):
        return

    monkeypatch.setattr(asyncio, "sleep", noop_sleep)

    result = await client.call("participant.kill", id=record["id"])
    assert result == {"id": record["id"], "killed": True}
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"


async def test_kill_finishes_running_jobs_as_killed(client, fake_tmux):
    """A child killed mid-job must end KILLED, not stranded RUNNING.

    Before the fix, _kill never touched jobs: the job row stayed RUNNING
    forever and the parent's await_sessions never woke. The kill path now
    finishes every still-running job targeting the killed participant with
    state KILLED after spawner.kill_pane succeeds.
    """
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "running"

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"
    assert job["error_code"] == "killed"


async def test_kill_wakes_the_awaiter_immediately(client, fake_tmux):
    """The job is terminal the moment the kill returns, not after a reaper tick.

    await_sessions returns the current job state; a KILLED job must read as
    terminal right away so the parent is not blocked until the reaper runs.
    """
    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    jobs = await client.call("jobs.await", handles=[handle], max_wait=1.0)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "killed"


async def test_kill_finishes_jobs_before_removing_worktree(daemon, client, fake_tmux, monkeypatch):
    """Jobs must finish before the worktree directory is deleted.

    Job completion hashes files in the worktree to record ``sha_after``; if
    the worktree is removed first, every path reads as gone and every touch
    row records a spurious deletion. This spy records the order of
    ``JobManager.finish`` and ``Spawner.retire`` and asserts finish came first.
    """
    from theater.daemon import spawner as spawner_mod

    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    order: list[str] = []

    original_finish = daemon.jobs.finish

    def spy_finish(*args, **kwargs):
        order.append("finish")
        return original_finish(*args, **kwargs)

    original_retire = spawner_mod.Spawner.retire

    def spy_retire(self, p, *, delete_branch):
        order.append("retire")
        return original_retire(self, p, delete_branch=delete_branch)

    monkeypatch.setattr(daemon.jobs, "finish", spy_finish)
    monkeypatch.setattr(spawner_mod.Spawner, "retire", spy_retire)

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"
    assert order.index("finish") < order.index("retire")


def _make_repo(tmp_path):
    """A real git repo with one commit, for worktree tests."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return str(root)


async def test_kill_of_worktree_child_preserves_non_null_sha_after(
    daemon, client, fake_tmux, tmp_path
):
    """A worktree child killed mid-job must record real sha_after, not NULL.

    The touch table records sha_after by hashing files in the child's worktree
    at job-finish time. If the worktree directory is deleted before the job
    finishes, every path reads as gone and every row gets sha_after=NULL — a
    spurious deletion. This end-to-end test uses a real git repo and worktree
    and asserts the touch row carries a real hash.
    """
    from theater.daemon.schema import touch as touch_table
    from theater.harness.base import EventPath

    repo_root = _make_repo(tmp_path)

    parent = await client.call("hello", harness="vibe", pane="%80", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd=repo_root,
        parent_id=parent["id"],
        worktree=True,
    )
    handle = child["handle"]

    # The worktree directory is the child's cwd. Write a file there and feed
    # it to the accumulator, the way the observer would for an event carrying
    # Event.paths.
    wt_cwd = child["cwd"]
    (Path(wt_cwd) / "touched.py").write_bytes(b"content")

    daemon.jobs.observe_paths(handle, (EventPath(path="touched.py", mode="write"),))

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"

    rows = list(daemon.store.conn.execute(touch_table.select()))
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["path"] == "touched.py"
    # sha_after must not be NULL — the worktree existed when finish ran.
    assert row["sha_after"] is not None


async def test_a_child_that_loses_its_pane_without_explicit_kill_crashes(
    client, fake_tmux, daemon, monkeypatch
):
    """A child whose pane vanishes on its own (not via kill) still finishes CRASHED.

    The explicit-kill marker only suppresses CRASHED for kills in flight;
    a self-exit must keep its old behaviour so the distinction between
    crashed and killed stays meaningful.
    """
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    handle = record["handle"]

    import theater.daemon.server as server_mod

    monkeypatch.setattr(server_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(server_mod.tmux, "run", _fake_list_panes(""))
    await daemon._reap_once()

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "crashed"
    assert job["error_code"] == "crashed"


async def test_the_reaper_notices_a_vanished_pane(daemon, client, fake_tmux, monkeypatch):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")

    from theater.tmux import client as tmux_client

    monkeypatch.setattr(tmux_client, "available", lambda: True)
    monkeypatch.setattr(tmux_client, "run", _fake_list_panes(""))
    await daemon._reap_once()

    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"


async def test_the_reaper_leaves_live_panes_alone(daemon, client, fake_tmux, monkeypatch):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")

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


async def test_spawn_created_event_marks_whether_a_prompt_was_sent(client, fake_tmux):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("spawn", harness="vibe", prompt="", approval="manual", cwd="/tmp")

    created = [e for e in await client.call("bus.tail") if e["kind"] == "participant.created"]
    assert [e["payload"]["has_prompt"] for e in created] == [True, False]


async def _await_events(client):
    return [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]


async def test_await_records_active_wait_edges(client, fake_tmux, monkeypatch):
    # Patch the announce delay rather than sleep it out: every test below is
    # about *which* rows an await writes, and a wall-clock threshold is flaky
    # on a loaded machine. The one test about timing patches it too, on both
    # sides of the wait.
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    jobs = await client.call(
        "jobs.await",
        handles=[child["handle"]],
        caller_id=parent["id"],
        max_wait=0.01,
    )

    assert jobs[0]["state"] == "running"
    await_events = await _await_events(client)
    assert [e["kind"] for e in await_events] == ["job.await.start", "job.await.end"]
    start, end = await_events
    assert start["from_id"] == parent["id"]
    assert start["to_id"] == child["id"]
    assert start["payload"]["handle"] == child["handle"]
    assert end["from_id"] == parent["id"]
    assert end["to_id"] == child["id"]
    assert end["payload"] == start["payload"]


async def test_await_records_one_pair_per_handle(client, fake_tmux, monkeypatch):
    """Two children, two edges — and every start closed exactly once."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    children = [
        await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        for _ in range(3)
    ]

    await client.call(
        "jobs.await",
        handles=[c["handle"] for c in children],
        caller_id=parent["id"],
        max_wait=0.01,
    )

    await_events = await _await_events(client)
    starts = [e for e in await_events if e["kind"] == "job.await.start"]
    ends = [e for e in await_events if e["kind"] == "job.await.end"]
    assert [e["payload"]["handle"] for e in starts] == [c["handle"] for c in children]
    assert [e["payload"]["handle"] for e in ends] == [c["handle"] for c in children]
    assert [e["to_id"] for e in starts] == [c["id"] for c in children]
    # One await, one token: the régie pairs an end to its start by it.
    assert len({e["payload"]["token"] for e in await_events}) == 1


async def test_await_that_returns_immediately_does_not_record_active_wait(
    client, fake_tmux, monkeypatch
):
    """A finished job is not something to be blocked on, delay or no delay."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    jobs = await client.call(
        "jobs.await",
        handles=[child["handle"]],
        caller_id=parent["id"],
        max_wait=1.0,
    )

    assert jobs[0]["state"] == "done"
    assert await _await_events(client) == []


async def test_await_with_one_finished_job_records_nothing(client, fake_tmux, monkeypatch):
    """One terminal job ends the whole call at entry — so no edge is live."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    running = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    # A promptless spawn has nothing to report, so its job is done on arrival.
    finished = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    await client.call(
        "jobs.await",
        handles=[running["handle"], finished["handle"]],
        caller_id=parent["id"],
        max_wait=1.0,
    )

    assert await _await_events(client) == []


async def test_short_await_does_not_announce(client, fake_tmux):
    """The polling case: `max_wait` under the threshold writes nothing.

    Runs against the real `AWAIT_ANNOUNCE_AFTER`, because the number is the
    point: an agent polling in a loop must not flood the bus, since `bus_tail`
    keeps only the newest rows and the flood would drop somebody else's
    `job.await.end`.
    """
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    for _ in range(3):
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=parent["id"],
            max_wait=0.02,
        )

    assert await _await_events(client) == []


async def test_await_announces_once_it_has_really_blocked(client, fake_tmux, monkeypatch):
    """The threshold, not the call, is what puts a row on the bus."""
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    call = {
        "handles": [child["handle"]],
        "caller_id": parent["id"],
        "max_wait": 0.05,
    }

    # Threshold above the wait: the caller gave up before the régie would ever
    # have been told it was waiting.
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 5.0)
    await client.call("jobs.await", **call)
    assert await _await_events(client) == []

    # Same call, threshold under the wait.
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    await client.call("jobs.await", **call)
    assert [e["kind"] for e in await _await_events(client)] == [
        "job.await.start",
        "job.await.end",
    ]


async def test_await_refused_by_the_rails_records_nothing(client, fake_tmux, monkeypatch):
    """A refused await never happened: no row for the régie to animate."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=child["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "cycle_detected"
    assert await _await_events(client) == []


async def test_await_that_raises_still_closes_its_starts(daemon, client, fake_tmux, monkeypatch):
    """An exception inside the wait must not strand the animation."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    async def boom(handles, max_wait=150.0):
        # Outlast the (patched-to-zero) announce delay, so the start rows are
        # on the bus before the wait falls over.
        await asyncio.sleep(0.05)
        raise RuntimeError("the store fell over mid-wait")

    monkeypatch.setattr(daemon.jobs, "await_jobs", boom)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=parent["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "internal"
    assert [e["kind"] for e in await _await_events(client)] == [
        "job.await.start",
        "job.await.end",
    ]


async def test_a_start_that_fails_halfway_still_closes_what_was_written(
    daemon, client, fake_tmux, monkeypatch
):
    """Half the start rows out, then the disk refuses — close those halves."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    children = [
        await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        for _ in range(3)
    ]

    real_append = daemon.store.bus_append
    starts = 0

    def flaky_append(kind, **kwargs):
        nonlocal starts
        if kind == "job.await.start":
            starts += 1
            if starts > 1:
                raise OSError("disk full")
        return real_append(kind, **kwargs)

    monkeypatch.setattr(daemon.store, "bus_append", flaky_append)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[c["handle"] for c in children],
            caller_id=parent["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "internal"

    monkeypatch.setattr(daemon.store, "bus_append", real_append)
    await_events = await _await_events(client)
    assert [e["kind"] for e in await_events] == ["job.await.start", "job.await.end"]
    assert await_events[0]["payload"] == await_events[1]["payload"]


# ---- harness name normalization ------------------------------------------


async def test_hello_normalizes_claude_code_to_claude(client):
    """A misreported harness name must not silently become unobservable."""
    me = await client.call("hello", harness="claude_code", pane="%1", cwd="/tmp")
    assert me["harness"] == "claude"


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
        return {"12345": ["vibe"], "12346": ["claude"], "12347": ["zsh"]}.get(str(pid), [])

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


# ---- runtime names --------------------------------------------------------


async def test_rename_over_rpc(client, fake_tmux):
    record = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    assert record["name"] is not None

    renamed = await client.call("participant.rename", id=record["id"], name="Truffaldino")
    assert renamed["name"] == "Truffaldino"

    fetched = await client.call("participants.get", id=record["id"])
    assert fetched["name"] == "Truffaldino"


async def test_rename_rejects_taken_name_over_rpc(client, fake_tmux):
    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.rename", id=b["id"], name=a["name"])
    assert exc.value.code == "name_taken"


async def test_send_addressed_by_name_reaches_the_right_target(client, fake_tmux, daemon):
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = "trusted-session"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    job = await client.call("send", target=target["name"], prompt="hello by name")
    assert job["state"] == "running"
    assert job["target_id"] == target["id"]
    assert len(fake_tmux.sent) == 1
    assert fake_tmux.sent[0] == ("%1", "hello by name")


async def test_kill_addressed_by_name_puts_id_in_explicit_kills(client, fake_tmux, daemon):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    name = record["name"]

    await client.call("participant.kill", id=name)

    assert record["id"] not in daemon._explicit_kills
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["name"] is None


# ---- live-only names contract ---------------------------------------------

_FIXED_NAME = "Brighella"


async def test_former_name_freed_and_successor_can_claim_it(client, fake_tmux):
    """After death the former name neither resolves nor blocks a successor."""
    first = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("participant.rename", id=first["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=first["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=_FIXED_NAME)
    assert exc.value.code == "not_found"

    successor = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    renamed = await client.call("participant.rename", id=successor["id"], name=_FIXED_NAME)
    assert renamed["name"] == _FIXED_NAME

    fetched = await client.call("participants.get", id=_FIXED_NAME)
    assert fetched["id"] == successor["id"]
    assert fetched["name"] == _FIXED_NAME
    assert fetched["status"] != "dead"


async def test_status_dead_frees_name_and_emits_canonical_death_event(client, fake_tmux):
    """participant.status DEAD frees the name and emits participant.dead, not participant.status."""
    record = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("participant.rename", id=record["id"], name=_FIXED_NAME)
    cursor = (await client.call("bus.tail", limit=1))[0]["id"]

    updated = await client.call("participant.status", id=record["id"], status="dead")
    assert updated["status"] == "dead"
    assert updated["name"] is None

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=_FIXED_NAME)
    assert exc.value.code == "not_found"

    events = await client.call("bus.tail", after_id=cursor)
    kinds = [e["kind"] for e in events if e.get("to_id") == record["id"]]
    assert "participant.dead" in kinds
    assert "participant.status" not in kinds


async def test_list_include_dead_returns_dead_rows_with_name_none(client, fake_tmux):
    """participants.list(include_dead=True) returns dead rows with name=None."""
    record = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    rows = await client.call("participants.list", include_dead=True)
    dead = [r for r in rows if r["id"] == record["id"]]
    assert len(dead) == 1
    assert dead[0]["status"] == "dead"
    assert dead[0]["name"] is None


async def test_read_transcript_by_dead_name_fails_not_found(client, fake_tmux):
    """read_transcript with a dead name fails at resolution before source access."""
    record = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("participant.rename", id=record["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=record["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("read_transcript", id=_FIXED_NAME, last_n=5)
    assert exc.value.code == "not_found"
