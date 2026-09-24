"""End-to-end daemon tests over a real Unix socket and a fake terminal provider."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from presence_fakes import FakePresence

from theater import harness as harness_registry
from theater import paths
from theater.daemon import methods
from theater.harness import HARNESSES
from theater.harness.contracts.runtime import RuntimeCompatibility
from theater.models import Participant, Status
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
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    assert me["tier"] == "external"
    assert me["addressable"] is False

    rows = await client.call("participants.list")
    assert [r["id"] for r in rows] == [me["id"]]


async def test_hello_is_idempotent_per_claimed_identity(client):
    a = await client.call("hello", id="participant-a", harness="vibe", cwd="/tmp")
    b = await client.call("hello", id="participant-a", harness="vibe", cwd="/tmp")
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


async def test_spawn_creates_an_identified_participant(client, terminal_provider, daemon):
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="say hello",
        approval="manual",
        cwd="/tmp",
        tmux_session="main",
    )

    assert record["tier"] == "spawned"
    assert record["tmux_pane"] is None
    assert record["addressable"] is True
    identified = await client.call("hello", id=record["id"], harness="vibe", cwd="/tmp")
    assert identified["addressable"] is True
    assert daemon.registry.addressable_count() == 1

    terminal = terminal_provider.creations[0]
    assert terminal["background"] is True
    assert terminal["command"] == [
        "vibe",
        "--agent=ask",
        "say hello",
    ]
    # The id must be reachable from inside the pane, and not only via the
    # environment, which the MCP SDK filters.
    assert record["id"] in terminal["env"]["VIBE_MCP_SERVERS"]


async def test_spawn_response_format_augments_and_persists_prompt(client, terminal_provider):
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

    assert terminal_provider.creations[0]["command"] == [
        "vibe",
        "--agent=ask",
        expected,
    ]
    assert expected.count("Return your final answer as a single bare JSON value") == 1
    job = await client.call("jobs.status", handle=record["handle"])
    assert job["prompt"] == expected
    assert job["response_format"] == serialized
    assert job["structured_result"] is None
    assert job["structured_status"] is None


async def test_promptless_spawn_with_empty_response_format_stays_running(client, terminal_provider):
    expected = _json_prompt("{}", "")
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        response_format={},
    )

    assert terminal_provider.creations[0]["command"] == [
        "vibe",
        "--agent=ask",
        expected,
    ]
    job = await client.call("jobs.status", handle=record["handle"])
    assert job["state"] == "running"
    assert job["prompt"] == expected
    assert job["response_format"] == "{}"


async def test_spawn_response_format_rejects_non_object_before_side_effects(
    client, terminal_provider
):
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
    assert terminal_provider.creations == []


async def test_spawn_response_format_refuses_resume_that_drops_prompt_before_side_effects(
    client, terminal_provider, monkeypatch
):
    from theater.harness import Harness, LaunchPlan
    from theater.harness.contracts.harness import LaunchParameterSupport

    class DropsPromptHarness(Harness):
        name = "drops-prompt-rpc"
        binary = "drops-prompt-rpc"
        resume_takes_prompt = False
        launch_parameter_support = LaunchParameterSupport(resume=True)

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
    assert terminal_provider.creations == []


async def test_a_freshly_spawned_participant_is_idle_before_hello(client, terminal_provider):
    """A spawned participant is IDLE from the moment it is created."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    assert record["status"] == "idle"

    fetched = await client.call("participants.get", id=record["id"])
    assert fetched["status"] == "idle"


async def test_spawn_writes_a_config_for_claude(client, terminal_provider):
    record = await client.call("spawn", harness="claude", prompt="hi", approval="yolo", cwd="/tmp")
    config = paths.mcp_config_path(record["id"])
    assert config.exists()
    assert record["id"] in config.read_text()
    assert "--dangerously-skip-permissions" in terminal_provider.creations[0]["command"]


async def test_spawn_requires_an_approval_mode(client, terminal_provider):
    with pytest.raises(RemoteError) as exc:
        await client.call("spawn", harness="vibe", prompt="hi", cwd="/tmp")
    assert exc.value.code == "bad_request"


async def test_spawn_rejects_an_unknown_harness(client, terminal_provider):
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


async def test_spawn_carries_a_model_all_the_way_to_the_pane(daemon, client, terminal_provider):
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
    assert terminal_provider.creations[0]["env"]["VIBE_ACTIVE_MODEL"] == "mysuperdupermodelname"

    await client.call(
        "spawn",
        harness="claude",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        model="opus-4.1",
    )
    assert "--model=opus-4.1" in terminal_provider.creations[1]["command"]


async def test_spawn_without_a_model_pins_the_vibe_env_empty(client, terminal_provider):
    """An unset variable would be inherited from the daemon's own environment."""
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    assert terminal_provider.creations[0]["env"]["VIBE_ACTIVE_MODEL"] == ""


async def test_spawn_refuses_an_impossible_model_before_creating_anything(
    daemon, client, terminal_provider, monkeypatch
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
    assert terminal_provider.creations == []


async def test_spawned_child_hellos_with_its_given_id(client, terminal_provider):
    child = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    # This is what the child's MCP server does on startup: no pane, no cwd it
    # can be trusted on, just the id from argv.
    seen = await client.call("hello", id=child["id"], harness="vibe", cwd="/tmp")

    assert seen["id"] == child["id"]
    assert seen["tier"] == "spawned"
    assert seen["tmux_pane"] == child["tmux_pane"]


async def test_lineage_shows_in_the_tree(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child1 = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    child2 = await client.call(
        "spawn",
        harness="claude",
        prompt="hi again",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    tree = await client.call("participants.tree")
    assert len(tree) == 1
    assert tree[0]["id"] == parent["id"]
    assert {c["id"] for c in tree[0]["children"]} == {child1["id"], child2["id"]}


async def test_kill_marks_dead_and_hides(client, terminal_provider):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    assert await client.call("participants.list") == []
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["addressable"] is False


async def test_kill_from_a_caller_who_is_the_parent_succeeds(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_refuses_a_target_that_is_not_the_callers_child(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_refuses_self_kill(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=parent["id"], caller_id=parent["id"])
    assert exc.value.code == "no_self_kill"
    alive = await client.call("participants.get", id=parent["id"])
    assert alive["status"] != "dead"


async def test_kill_on_an_already_dead_child_is_a_no_op(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_without_caller_id_is_unrestricted(client, terminal_provider, daemon):
    """The CLI and the régie send no caller_id; a human may kill anything."""
    daemon.presence = FakePresence()
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_finishes_running_jobs_as_killed(client, terminal_provider):
    """A child killed mid-job must end KILLED, not stranded RUNNING.

    Before the fix, _kill never touched jobs: the job row stayed RUNNING
    forever and the parent's await_sessions never woke. The kill path now
    finishes every still-running job targeting the killed participant with
    state KILLED after spawner.kill_pane succeeds.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_wakes_the_awaiter_immediately(client, terminal_provider):
    """The job is terminal the moment the kill returns, not after a reaper tick.

    await_sessions returns the current job state; a KILLED job must read as
    terminal right away so the parent is not blocked until the reaper runs.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_kill_finishes_jobs_before_releasing_workspace_usage(
    daemon, client, terminal_provider, monkeypatch
):
    """Jobs finish before durable workspace usage is released.

    Job completion hashes files in the worktree to record ``sha_after``; if
    usage were released first, explicit cleanup could remove the files before
    their final hashes are recorded.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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

    original_release = daemon.spawner.release_workspace_usage

    def spy_release(p, *, reason):
        order.append("release")
        return original_release(p, reason=reason)

    monkeypatch.setattr(daemon.jobs, "finish", spy_finish)
    monkeypatch.setattr(daemon.spawner, "release_workspace_usage", spy_release)

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"
    assert order.index("finish") < order.index("release")


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
    daemon, client, terminal_provider, tmp_path
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

    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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
    assert Path(wt_cwd).is_dir(), "participant exit must retain the worktree"


def _fake_inventory_empty():
    async def observe_inventory():
        return None

    return observe_inventory


async def test_bus_records_the_story(client, terminal_provider):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    kinds = [e["kind"] for e in await client.call("bus.tail")]
    assert "participant.created" in kinds
    assert "participant.pane" not in kinds


async def test_spawn_created_event_marks_whether_a_prompt_was_sent(client, terminal_provider):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("spawn", harness="vibe", prompt="", approval="manual", cwd="/tmp")

    created = [e for e in await client.call("bus.tail") if e["kind"] == "participant.created"]
    assert [e["payload"]["has_prompt"] for e in created] == [True, False]


async def _await_events(client):
    return [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]


async def test_await_records_active_wait_edges(client, terminal_provider, monkeypatch, daemon):
    # Patch the announce delay rather than sleep it out: every test below is
    # about *which* rows an await writes, and a wall-clock threshold is flaky
    # on a loaded machine. The one test about timing patches it too, on both
    # sides of the wait.
    daemon.presence = FakePresence()
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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
    assert end["payload"]["handle"] == start["payload"]["handle"]
    assert end["payload"]["token"] == start["payload"]["token"]
    assert end["payload"]["state"] == "timeout"
    assert end["payload"]["elapsed_seconds"] >= 0


async def test_await_records_one_pair_per_handle(client, terminal_provider, monkeypatch, daemon):
    """Two children, two edges — and every start closed exactly once."""
    daemon.presence = FakePresence()
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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
    client, terminal_provider, monkeypatch
):
    """A finished job is not something to be blocked on, delay or no delay."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_await_with_one_finished_job_records_nothing(client, terminal_provider, monkeypatch):
    """One terminal job ends the whole call at entry — so no edge is live."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_short_await_does_not_announce(client, terminal_provider):
    """The polling case: `max_wait` under the threshold writes nothing.

    Runs against the real `AWAIT_ANNOUNCE_AFTER`, because the number is the
    point: an agent polling in a loop must not flood the bus, since `bus_tail`
    keeps only the newest rows and the flood would drop somebody else's
    `job.await.end`.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_await_announces_once_it_has_really_blocked(client, terminal_provider, monkeypatch):
    """The threshold, not the call, is what puts a row on the bus."""
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_await_refused_by_the_rails_records_nothing(client, terminal_provider, monkeypatch):
    """A refused await never happened: no row for the régie to animate."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_await_that_raises_still_closes_its_starts(
    daemon, client, terminal_provider, monkeypatch
):
    """An exception inside the wait must not strand the animation."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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
    daemon, client, terminal_provider, monkeypatch
):
    """Half the start rows out, then the disk refuses — close those halves."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
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
    assert await_events[0]["payload"]["handle"] == await_events[1]["payload"]["handle"]
    assert await_events[0]["payload"]["token"] == await_events[1]["payload"]["token"]
    assert await_events[1]["payload"]["state"] == "error"
    assert await_events[1]["payload"]["elapsed_seconds"] >= 0


# ---- harness name normalization ------------------------------------------


async def test_hello_normalizes_claude_code_to_claude(client):
    """A misreported harness name must not silently become unobservable."""
    me = await client.call("hello", harness="claude_code", cwd="/tmp")
    assert me["harness"] == "claude"


async def test_unknown_harness_name_passes_through(client, terminal_provider):
    """A genuinely unknown harness is not rejected — just unobservable."""
    me = await client.call("hello", harness="cursor", cwd="/tmp")
    assert me["harness"] == "cursor"


# Provider adoption and inventory behavior is covered by RC10 provider tests.

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


async def test_harnesses_reports_daemon_native_compatibility(client, daemon, monkeypatch):
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: f"/bin/{binary}")

    async def probe(name, callback, context, *, configuration):
        del name, context, configuration
        if callback.__name__ == "probe_claude_native_compatibility":
            return RuntimeCompatibility(
                supported=False,
                policy="claude-messaging-native-controls-2.1.248",
                native_version="2.1.220",
                reason="below floor",
            )
        return RuntimeCompatibility(
            supported=True,
            policy="qualified-test",
            native_version="1.0.0",
        )

    monkeypatch.setattr(daemon.compatibility_probes, "probe", probe)
    rows = {row["name"]: row for row in await client.call("harnesses")}

    assert rows["claude"]["native_compatibility"]["status"] == "outside-qualified-range"
    assert rows["claude"]["native_compatibility"]["qualified_range"] == ">=2.1.248"
    assert rows["codex"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["opencode"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["pi"]["native_compatibility"]["status"] == "native-compatible"
    assert rows["vibe"]["native_compatibility"]["status"] == "legacy-only"


# ---- runtime names --------------------------------------------------------


async def test_rename_over_rpc(client, terminal_provider):
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    assert record["name"] is not None

    renamed = await client.call("participant.rename", id=record["id"], name="Truffaldino")
    assert renamed["name"] == "Truffaldino"

    fetched = await client.call("participants.get", id=record["id"])
    assert fetched["name"] == "Truffaldino"


async def test_rename_rejects_taken_name_over_rpc(client, terminal_provider):
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    b = await client.call("hello", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.rename", id=b["id"], name=a["name"])
    assert exc.value.code == "name_taken"


async def test_send_addressed_by_name_reaches_the_right_target(client, terminal_provider, daemon):
    target = await client.call("hello", harness="vibe", cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = "trusted-session"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    terminal_id = terminal_provider.bind(daemon, participant.id)
    job = await client.call("send", target=target["name"], prompt="hello by name")
    assert job["state"] == "running"
    assert job["target_id"] == target["id"]
    assert len(terminal_provider.deliveries) == 1
    assert terminal_provider.deliveries[0] == (terminal_id, "hello by name")


async def test_kill_addressed_by_name_puts_id_in_explicit_kills(client, terminal_provider, daemon):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    name = record["name"]

    await client.call("participant.kill", id=name)

    assert record["id"] not in daemon._explicit_kills
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["name"] is None


# ---- live-only names contract ---------------------------------------------

_FIXED_NAME = "Brighella"


async def test_former_name_freed_and_successor_can_claim_it(client, terminal_provider, daemon):
    """After death the former name neither resolves nor blocks a successor."""
    daemon.presence = FakePresence()
    first = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.rename", id=first["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=first["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=_FIXED_NAME)
    assert exc.value.code == "not_found"

    successor = await client.call("hello", harness="vibe", cwd="/tmp")
    renamed = await client.call("participant.rename", id=successor["id"], name=_FIXED_NAME)
    assert renamed["name"] == _FIXED_NAME

    fetched = await client.call("participants.get", id=_FIXED_NAME)
    assert fetched["id"] == successor["id"]
    assert fetched["name"] == _FIXED_NAME
    assert fetched["status"] != "dead"


async def test_status_dead_frees_name_and_emits_canonical_death_event(
    client, terminal_provider, daemon
):
    """participant.status DEAD frees the name and emits participant.dead, not participant.status."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
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


async def test_list_include_dead_returns_dead_rows_with_name_none(
    client, terminal_provider, daemon
):
    """participants.list(include_dead=True) returns dead rows with name=None."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    rows = await client.call("participants.list", include_dead=True)
    dead = [r for r in rows if r["id"] == record["id"]]
    assert len(dead) == 1
    assert dead[0]["status"] == "dead"
    assert dead[0]["name"] is None


async def test_read_transcript_by_dead_name_fails_not_found(client, terminal_provider, daemon):
    """read_transcript with a dead name fails at resolution before source access."""
    daemon.presence = FakePresence()
    record = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("participant.rename", id=record["id"], name=_FIXED_NAME)
    await client.call("participant.kill", id=record["id"])

    with pytest.raises(RemoteError) as exc:
        await client.call("read_transcript", id=_FIXED_NAME)
    assert exc.value.code == "not_found"


# ---- phase 3: spawn reserve/launch ordering -----------------------------


async def test_promptless_spawn_stays_done_after_provider_binding(
    client, terminal_provider, daemon
):
    """A promptless spawn resolves the job DONE after launch succeeds.

    The reserve/launch split must preserve the promptless semantics: the job
    is created after reserve, launch creates the pane, and only then is the
    job finished DONE — because a launch failure must leave the job CRASHED,
    not DONE.
    """
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
    )
    await asyncio.gather(*daemon.operation_service.owned_tasks)
    job = await client.call("jobs.status", handle=record["handle"])
    assert job["state"] == "done"
    assert job["error_code"] is None
    events = [
        event
        for group in daemon.store.journal.groups_after(0, limit=500)
        for event in group.events
        if event.kind == "job.updated" and event.entity_id == record["handle"]
    ]
    assert events[-1].payload["state"] == job["state"]
    assert events[-1].payload["raw_result"] == job["result"] == ""


async def test_promptless_spawn_job_running_during_launch(
    client, terminal_provider, daemon, monkeypatch
):
    """For a promptless spawn, the job is RUNNING (not DONE) during launch.

    The job is created after reserve and stays RUNNING while launch creates
    the pane. It is finished DONE only after launch succeeds, so a launch
    failure leaves the job CRASHED rather than DONE.
    """
    original_request = daemon.terminal_service.connections.request
    captured: dict = {}

    async def spy_request(provider_id, generation, method, params):
        if method == "terminal.create":
            captured["job_at_launch"] = daemon.jobs.get(params["participant_id"])
        return await original_request(provider_id, generation, method, params)

    monkeypatch.setattr(daemon.terminal_service.connections, "request", spy_request)
    await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
    )
    await asyncio.gather(*daemon.operation_service.owned_tasks)

    job = captured.get("job_at_launch")
    assert job is not None, "job must exist when terminal.create is dispatched"
    assert job.state == "running", "promptless spawn job must be RUNNING during launch"


async def test_promptless_launch_failure_leaves_crashed_job(
    client, terminal_provider, daemon, monkeypatch
):
    """A promptless spawn whose launch fails must leave the job CRASHED.

    Before the fix, the promptless job was finished DONE before launch ran,
    so a launch failure left a DONE job for a participant with no pane —
    the caller would see a successful result for a spawn that never
    launched. Now the DONE finish is deferred until after launch succeeds,
    so a launch failure leaves the job CRASHED with spawn_failed.
    """

    async def reject(_provider_id, generation, _method, params):
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "outcome": "rejected",
            "error": {"code": "provider_busy", "message": "fixture rejection"},
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", reject)
    with pytest.raises(RemoteError):
        await client.call(
            "spawn",
            harness="vibe",
            prompt="",
            approval="manual",
            cwd="/tmp",
        )
    await asyncio.gather(*daemon.operation_service.owned_tasks)

    rows = await client.call("participants.list", include_dead=True)
    assert len(rows) == 1
    assert rows[0]["status"] == "dead"

    pid = rows[0]["id"]
    job = daemon.jobs.get(pid)
    assert job is not None
    assert job.state == "crashed", "promptless launch failure must CRASH the job, not DONE"
    assert job.error_code == "provider_busy"


# ---- participants.list: ids filter (RPC level) ----------------------------


async def test_list_ids_omitted_returns_all(client):
    """ids omitted => response identical to today (all live rows)."""
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    b = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    ids = [r["id"] for r in rows]
    assert a["id"] in ids
    assert b["id"] in ids


async def test_list_ids_subset_returns_exact_rows(client):
    """ids=[a, c] out of several participants => exactly those rows."""
    a = await client.call("hello", harness="vibe", cwd="/tmp")
    await client.call("hello", harness="vibe", cwd="/tmp")
    c = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list", ids=[a["id"], c["id"]])
    assert [r["id"] for r in rows] == [a["id"], c["id"]]


async def test_list_ids_empty_returns_nothing(client):
    """ids=[] is the trap: must return [] not everything."""
    await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list", ids=[])
    assert rows == []


async def test_list_ids_unknown_silently_omitted(client):
    """Unknown ids are dropped, no error."""
    rows = await client.call("participants.list", ids=["ghost-123"])
    assert rows == []


async def test_list_ids_not_a_list_is_bad_request(client):
    """ids must be a list."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids="abc")
    assert exc.value.code == "bad_request"


async def test_list_ids_element_not_string_is_bad_request(client):
    """Any non-string element is rejected."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[1])
    assert exc.value.code == "bad_request"


async def test_list_ids_empty_string_element_is_bad_request(client):
    """An empty string element must not silently widen the query."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[""])
    assert exc.value.code == "bad_request"


async def test_list_ids_over_200_is_bad_request(client):
    """More than 200 ids at once is refused."""
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[f"x-{i}" for i in range(201)])
    assert exc.value.code == "bad_request"


async def test_list_ids_dead_excluded_without_include_dead(client, terminal_provider):
    """A dead id is omitted when include_dead=False, even when named explicitly."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])
    rows = await client.call("participants.list", ids=[record["id"]])
    assert rows == []


async def test_list_ids_dead_returned_with_include_dead(client, terminal_provider):
    """A dead id is returned when include_dead=True."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])
    rows = await client.call("participants.list", ids=[record["id"]], include_dead=True)
    assert len(rows) == 1
    assert rows[0]["id"] == record["id"]
    assert rows[0]["status"] == "dead"


async def test_list_live_default_remains_unbounded(client, daemon):
    for index in range(101):
        daemon.store.upsert_participant(
            Participant(id=f"live-{index:03}", harness="vibe", created_at=float(index))
        )

    rows = await client.call("participants.list")
    assert [row["id"] for row in rows] == [f"live-{index:03}" for index in range(101)]


async def test_list_include_dead_is_unbounded_without_explicit_limit(client, daemon):
    for index in range(101):
        daemon.store.upsert_participant(
            Participant(
                id=f"dead-{index:03}",
                harness="vibe",
                status=Status.DEAD,
                created_at=float(index),
            )
        )

    all_rows = await client.call("participants.list", include_dead=True)
    first = await client.call("participants.list", include_dead=True, limit=100)
    second = await client.call(
        "participants.list",
        include_dead=True,
        limit=100,
        after_id=first[-1]["id"],
    )
    end = await client.call(
        "participants.list",
        include_dead=True,
        limit=100,
        after_id=second[-1]["id"],
    )

    assert [row["id"] for row in all_rows] == [f"dead-{index:03}" for index in range(101)]
    assert [row["id"] for row in first] == [f"dead-{index:03}" for index in range(100)]
    assert [row["id"] for row in second] == ["dead-100"]
    assert end == []


@pytest.mark.parametrize("limit", [True, "10", 0, 201])
async def test_list_rejects_invalid_page_limit(client, limit):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", limit=limit)
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("after_id", ["", 1])
async def test_list_rejects_invalid_after_id(client, after_id):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", after_id=after_id)
    assert exc.value.code == "bad_request"


async def test_list_rejects_missing_keyset_cursor(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", after_id="gone")
    assert exc.value.code == "bad_request"
    assert "restart pagination" in str(exc.value)


@pytest.mark.parametrize("params", [{"limit": 1}, {"after_id": "p-any"}])
async def test_list_rejects_pagination_with_ids(client, params):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.list", ids=[], **params)
    assert exc.value.code == "bad_request"


async def test_list_keyset_composes_with_direct_children_and_dead_rows(client, daemon):
    parent = Participant(id="parent", harness="vibe", created_at=0.0)
    child_a = Participant(
        id="child-a",
        harness="vibe",
        parent_id=parent.id,
        status=Status.DEAD,
        created_at=1.0,
    )
    child_b = Participant(
        id="child-b",
        harness="vibe",
        parent_id=parent.id,
        status=Status.DEAD,
        created_at=2.0,
    )
    outsider = Participant(id="outsider", harness="vibe", status=Status.DEAD, created_at=3.0)
    for participant in (parent, child_a, child_b, outsider):
        daemon.store.upsert_participant(participant)

    first = await client.call(
        "participants.list",
        include_dead=True,
        parent_id=parent.id,
        limit=1,
    )
    second = await client.call(
        "participants.list",
        include_dead=True,
        parent_id=parent.id,
        limit=1,
        after_id=first[-1]["id"],
    )

    assert [row["id"] for row in first] == [child_a.id]
    assert [row["id"] for row in second] == [child_b.id]


async def test_list_parent_filter_returns_direct_children_only(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="child",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    grandchild = await client.call(
        "spawn",
        harness="vibe",
        prompt="grandchild",
        approval="manual",
        cwd="/tmp",
        parent_id=child["id"],
    )
    await client.call("hello", harness="vibe", cwd="/tmp")

    rows = await client.call("participants.list", parent_id=parent["id"])
    assert [row["id"] for row in rows] == [child["id"]]
    assert grandchild["id"] not in {row["id"] for row in rows}


async def test_list_parent_filter_composes_with_ids_and_include_dead(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="child",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    sibling = await client.call(
        "spawn",
        harness="vibe",
        prompt="sibling",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    outsider = await client.call(
        "spawn",
        harness="vibe",
        prompt="outsider",
        approval="manual",
        cwd="/tmp",
    )
    await client.call("participant.kill", id=child["id"])

    live_rows = await client.call(
        "participants.list",
        parent_id=parent["id"],
        ids=[child["id"], sibling["id"], outsider["id"]],
    )
    assert [row["id"] for row in live_rows] == [sibling["id"]]

    all_rows = await client.call(
        "participants.list",
        parent_id=parent["id"],
        ids=[child["id"], sibling["id"], outsider["id"]],
        include_dead=True,
    )
    assert [row["id"] for row in all_rows] == [child["id"], sibling["id"]]


# ---- participants.list: resume_state (RPC level) --------------------------


async def test_list_resume_state_live(client):
    """A live participant reports resume_state == 'live'."""
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    row = next(r for r in rows if r["id"] == me["id"])
    assert row["resume_state"] == "live"


async def test_list_resume_state_no_session_id(client, terminal_provider):
    """Dead participant with no session_id => no_session_id."""
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    pid = record["id"]
    await client.call("participant.kill", id=pid)
    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    # session_id is not set by the fake tmux observer, so it remains None.
    assert row["resume_state"] == "no_session_id"


async def test_list_resume_state_untrusted(client, daemon):
    """Dead, has session_id, but heuristic provenance => untrusted."""
    p = await client.call("hello", harness="vibe", cwd="/tmp")
    pid = p["id"]
    participant = daemon.registry.get(pid)
    participant.session_id = "sess-heuristic"
    participant.session_correlation = "heuristic"
    daemon.store.upsert_participant(participant)
    daemon.registry.mark_dead(pid)

    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    assert row["resume_state"] == "untrusted"


async def test_list_resume_state_resumable(client, daemon):
    """Dead, has session_id, trusted provenance, no live owner => resumable."""
    p = await client.call("hello", harness="vibe", cwd="/tmp")
    pid = p["id"]
    participant = daemon.registry.get(pid)
    participant.session_id = "sess-trusted"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    daemon.registry.mark_dead(pid)

    rows = await client.call("participants.list", include_dead=True)
    row = next(r for r in rows if r["id"] == pid)
    assert row["resume_state"] == "resumable"


async def test_list_resume_state_owned_by_live(client, daemon):
    """Dead trusted row AND live trusted row sharing session id => owned_by_live."""
    # Dead participant with trusted binding.
    dead_p = await client.call("hello", harness="vibe", cwd="/tmp")
    dead_id = dead_p["id"]
    dead_part = daemon.registry.get(dead_id)
    dead_part.session_id = "sess-shared"
    dead_part.session_correlation = "operator"
    dead_part.created_at = 0.0
    daemon.store.upsert_participant(dead_part)
    daemon.registry.mark_dead(dead_id)

    # Live participant with the same harness + session_id at trusted provenance.
    live_p = await client.call("hello", harness="vibe", cwd="/tmp")
    live_id = live_p["id"]
    live_part = daemon.registry.get(live_id)
    live_part.session_id = "sess-shared"
    live_part.session_correlation = "operator"
    daemon.store.upsert_participant(live_part)

    rows = await client.call("participants.list", include_dead=True, limit=1)
    assert [row["id"] for row in rows] == [dead_id]
    dead_row = rows[0]
    assert dead_row["resume_state"] == "owned_by_live"


async def test_list_resume_state_owned_by_live_beats_untrusted(client, daemon):
    """An untrusted dead row with a trusted live peer reports owned_by_live, not untrusted.

    The spawner's _validate_resume_identity filters to trusted participants only,
    so the untrusted dead row is invisible to it.  The live trusted peer triggers
    the live-owner gate regardless of the subject row's own provenance.  This
    test is the key regression guard for the precedence inversion bug.
    """
    # Dead participant with UNTRUSTED provenance.
    dead_p = await client.call("hello", harness="vibe", cwd="/tmp")
    dead_id = dead_p["id"]
    dead_part = daemon.registry.get(dead_id)
    dead_part.session_id = "sess-mixed"
    dead_part.session_correlation = "heuristic"  # untrusted
    daemon.store.upsert_participant(dead_part)
    daemon.registry.mark_dead(dead_id)

    # Live participant with the same harness + session_id at TRUSTED provenance.
    live_p = await client.call("hello", harness="vibe", cwd="/tmp")
    live_id = live_p["id"]
    live_part = daemon.registry.get(live_id)
    live_part.session_id = "sess-mixed"
    live_part.session_correlation = "operator"
    daemon.store.upsert_participant(live_part)

    rows = await client.call("participants.list", include_dead=True)
    dead_row = next(r for r in rows if r["id"] == dead_id)
    # Must be owned_by_live, not untrusted.
    assert dead_row["resume_state"] == "owned_by_live"


async def test_list_no_internal_fields_exposed(client):
    """Internal observation fields must not appear in participants.list."""
    await client.call("hello", harness="vibe", cwd="/tmp")
    rows = await client.call("participants.list")
    for row in rows:
        assert "session_correlation" not in row
        assert "transcript_domain" not in row
        assert "transcript_location" not in row
        assert "resume_floor" not in row
        assert "source_checkpoint" not in row


async def test_workers_shutdown_drains_before_returning(daemon):
    """workers.shutdown() must drain in-flight workers before returning,
    so a replacement daemon cannot acquire the lock while the old daemon's
    git operations are still running."""
    from theater.daemon import workers

    workers._executor = None
    executor = workers._get_executor()
    started = asyncio.Event()
    finished = False

    def slow_task():
        nonlocal finished
        started.set()
        import time

        time.sleep(0.3)
        finished = True
        return "done"

    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(executor, slow_task)
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await workers.shutdown()
    assert finished, "shutdown must drain in-flight workers before returning"
    assert workers._executor is None

    result = await fut
    assert result == "done"
