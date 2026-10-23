"""Spawning over RPC: argument checks, models, response formats and launch ordering."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from sqlalchemy import update

from tests.support.handoff_spawn import end_source, init_repository, launch_all, start_source
from theater import paths
from theater.daemon.rpc.spawning import _PrivateSpawnMapping
from theater.daemon.schema import jobs as jobs_table
from theater.harness import HARNESSES
from theater.harness.contracts.runtime import RuntimeWiring
from theater.models import JobState
from theater.protocol import RemoteError

_JSON_SCHEMA_PREFIX = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)


def test_private_spawn_mapping_keeps_omission_distinct_from_null():
    base: dict[str, object] = {"harness": "vibe", "approval": "manual", "cwd": "/tmp"}
    omitted = _PrivateSpawnMapping(base, "vibe", False, RuntimeWiring.AUTO, None)
    explicit = _PrivateSpawnMapping(
        {**base, "background": None, "window_name": None, "tmux_session": None},
        "vibe",
        False,
        RuntimeWiring.AUTO,
        None,
    )
    assert omitted.provider_request("") == explicit.provider_request("")
    omitted_payload = omitted.deferred_payload()
    explicit_payload = explicit.deferred_payload()
    for key in ("background", "window_name", "tmux_session"):
        assert key not in omitted_payload
        assert key in explicit_payload
        assert explicit_payload[key] is None


def _json_prompt(schema: str, prompt: str) -> str:
    return f"{_JSON_SCHEMA_PREFIX.format(schema=schema)}\n\n{prompt}"


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
    # argv[0] is the resolved binary (the fixture maps harness CLIs to sys.executable),
    # not a bare name the pane would re-resolve on its own PATH.
    assert terminal["command"] == [
        sys.executable,
        "--agent=ask",
        "--",
        "say hello",
    ]
    # The id must be reachable from inside the pane, and not only via the
    # environment, which the MCP SDK filters.
    assert record["id"] in terminal["env"]["VIBE_MCP_SERVERS"]


async def test_deferred_spawn_keeps_accepted_presentation_input_out_of_launch(
    client, daemon, terminal_provider, tmp_path
):
    repo = init_repository(tmp_path)
    start_source(daemon, repo)
    record = await client.call(
        "spawn",
        harness="vibe",
        prompt="continue",
        approval="manual",
        cwd=str(repo),
        after="Writer",
        background=False,
        window_name="custom-window",
        tmux_session="custom-session",
    )
    stored = json.loads(daemon.store.get_handoff_gate(record["handle"]).request)["params"]
    assert "background" not in stored
    assert "window_name" not in stored
    assert "tmux_session" not in stored
    assert terminal_provider.creations == []

    await end_source(daemon, state=JobState.DONE, result="ready")
    await launch_all(daemon)
    presentation = terminal_provider.creations[0]
    assert presentation["background"] is True
    assert presentation["name"] == f"vibe-{record['id'][:6]}"


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
        sys.executable,
        "--agent=ask",
        "--",
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
        sys.executable,
        "--agent=ask",
        "--",
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


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"approval": None}, None, id="missing-approval-mode"),
        pytest.param(
            {"approval": "auto"},
            "approval must be one of manual|edits|yolo, got 'auto'",
            id="unknown-approval-value",
        ),
        pytest.param({"harness": "cursor"}, None, id="unknown-harness"),
    ],
)
async def test_spawn_rejects_bad_arguments(client, terminal_provider, overrides, message):
    arguments = {"harness": "vibe", "prompt": "hi", "approval": "manual", "cwd": "/tmp"}
    arguments.update(overrides)
    arguments = {key: value for key, value in arguments.items() if value is not None}
    with pytest.raises(RemoteError) as exc:
        await client.call("spawn", **arguments)
    assert exc.value.code == "bad_request"
    if message is not None:
        assert message in str(exc.value)


async def test_spawn_worktree_outside_git_names_child_repos(client, terminal_provider, tmp_path):
    (tmp_path / "older" / ".git").mkdir(parents=True)
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / ".git").write_text("gitdir: /elsewhere\n")
    (tmp_path / "plain").mkdir()
    (tmp_path / ".hidden" / ".git").mkdir(parents=True)
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn", harness="vibe", approval="manual", cwd=str(tmp_path), worktree=True
        )
    assert exc.value.code == "bad_request"
    assert "[" in str(exc.value) and "changing harness will not help" in str(exc.value)
    named = str(exc.value).split("[", 1)[1].split("]", 1)[0]
    assert sorted(named.split(", ")) == ["linked", "older"]
    assert terminal_provider.creations == []


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

    wakes: list[asyncio.Event] = []
    listeners: list[str] = []
    daemon.jobs.add_terminal_listener(listeners.append)
    finished_bus_before = [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"]

    async def reject(_provider_id, generation, _method, params):
        handle = params["participant_id"]
        with daemon.store.write_unit() as unit:
            unit.connection.execute(
                update(jobs_table)
                .where(jobs_table.c.handle == handle)
                .values(
                    result="prior result",
                    response_format="json",
                    structured_result='{"answer":"old"}',
                    structured_status="parsed",
                )
            )
        daemon.jobs.track(handle)
        wakes.append(daemon.jobs._events[handle])
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
    assert (job.result, job.response_format, job.structured_result, job.structured_status) == (
        "prior result",
        "json",
        '{"answer":"old"}',
        "parsed",
    )
    assert len(wakes) == 1 and wakes[0].is_set() and listeners == []
    assert [row for row in daemon.store.bus_tail() if row["kind"] == "job.finished"] == (
        finished_bus_before
    )
