"""Tests for provider and native live delivery.

Covers: send to an addressable target, human_present rejection, busy
rejection, send to an unaddressable target, and the full send → await →
result loop.

The terminal provider is an in-process callback double.
The job lifecycle is real — the observer's _answer_turn would
finish the send job if it were connected to a real transcript, but here
the tests finish jobs directly via the JobManager.
"""

from __future__ import annotations

import json

import pytest
from shipped import VibeHarness

from tests._presence_doubles import AbsentPresence, UnknownPresence
from theater.daemon.jobs import JobState
from theater.daemon.schema import jobs
from theater.harness import HARNESSES
from theater.models import Status, Tier
from theater.protocol import RemoteError

_JSON_SCHEMA_PREFIX = (
    "Return your final answer as a single bare JSON value (no code fences, no prose) "
    "matching this schema hint: {schema}"
)


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    """No human focus: the composed gates pass for every test in this module."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


def _json_prompt(schema: str, prompt: str) -> str:
    return f"{_JSON_SCHEMA_PREFIX.format(schema=schema)}\n\n{prompt}"


def _trust(daemon, participant_id: str, *, provenance: str = "operator") -> None:
    participant = daemon.registry.get(participant_id)
    participant.session_id = f"session-{participant_id}"
    participant.session_correlation = provenance
    daemon.store.upsert_participant(participant)


async def _target(client, daemon, *, pane: str = "%1", harness: str = "vibe"):
    del pane
    target = await client.call("hello", harness=harness, pane=None, cwd="/tmp")
    _trust(daemon, target["id"])
    daemon._test_terminal_provider.bind(daemon, target["id"], command=harness)
    return target


def _vibe_session(root, short: str, cwd, *, text: str = "hello"):
    d = root / f"session_20260816_191459_{short}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "session_id": f"{short}-1111-2222-3333",
                "environment": {"working_directory": str(cwd)},
            }
        ),
        encoding="utf-8",
    )
    messages = d / "messages.jsonl"
    messages.write_text(json.dumps({"role": "assistant", "content": text}) + "\n")
    return messages


async def test_send_creates_a_running_job(client, terminal_provider, daemon):
    """send to an addressable target creates a job and delivers the prompt."""
    target = await _target(client, daemon)
    job = await client.call("send", target=target["id"], prompt="do the thing")
    assert job["state"] == "running"
    assert job["kind"] == "send"
    assert job["prompt"] == "do the thing"
    assert job["target_id"] == target["id"]
    # The prompt was delivered via send-keys to the right pane
    assert len(terminal_provider.deliveries) == 1
    binding = daemon.store.terminal_bindings.get(target["id"])
    assert terminal_provider.deliveries[0] == (binding.terminal_id, "do the thing")


@pytest.mark.parametrize(
    ("harness", "pane"),
    [("claude", "%7"), ("vibe", "%8"), ("opencode", "%9")],
)
async def test_adopted_untrusted_transcript_harness_refuses_send_without_job(
    client, terminal_provider, daemon, harness, pane
):
    target = await client.call("hello", harness=harness, pane=None, cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.tier = Tier.ADOPTED
    daemon.store.upsert_participant(participant)
    terminal_provider.bind(daemon, target["id"], command=harness, terminal_id=pane)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="do the thing")

    assert exc.value.code == "transcript_untrusted"
    assert f"theater candidates {target['id']}" in exc.value.message
    assert f"theater bind {target['id']} <candidate> --confirm-id {target['id']}" in (
        exc.value.message
    )
    assert daemon.store.running_jobs_for_target(target["id"]) == []
    rows = daemon.store.conn.execute(
        jobs.select().where(jobs.c.target_id == target["id"])
    ).fetchall()
    assert rows == []
    assert terminal_provider.deliveries == []
    assert daemon.store.refusal_counts() == {"transcript_untrusted": 1}


async def test_adopted_codex_with_proven_process_correlation_can_send(
    client, terminal_provider, daemon
):
    target = await client.call("hello", harness="codex", pane=None, cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.tier = Tier.ADOPTED
    daemon.store.upsert_participant(participant)
    terminal_provider.bind(daemon, target["id"], command="codex", terminal_id="%7")
    _trust(daemon, target["id"], provenance="proven")

    job = await client.call("send", target=target["id"], prompt="do the thing")

    assert job["state"] == "running"
    assert terminal_provider.deliveries == [("%7", "do the thing")]


async def test_transcript_identity_lost_refuses_send_before_job_creation(
    client, terminal_provider, daemon
):
    target = await _target(client, daemon)
    daemon.observer.mark_transcript_identity_lost(target["id"], "positive watcher evidence")

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="do not create")

    assert exc.value.code == "transcript_identity_lost"
    assert f"theater candidates {target['id']}" in exc.value.message
    assert f"theater bind {target['id']} <candidate> --confirm-id {target['id']}" in (
        exc.value.message
    )
    assert daemon.store.running_jobs_for_target(target["id"]) == []
    rows = daemon.store.conn.execute(
        jobs.select().where(jobs.c.target_id == target["id"])
    ).fetchall()
    assert rows == []
    assert terminal_provider.deliveries == []


async def test_adopted_vibe_send_and_history_work_after_operator_bind(
    client, terminal_provider, daemon, tmp_path, monkeypatch
):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    candidate = _vibe_session(root, "bind0001", project, text="BOUND")
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    target = await client.call("hello", harness="vibe", pane=None, cwd=str(project))
    participant = daemon.registry.get(target["id"])
    participant.tier = Tier.ADOPTED
    daemon.store.upsert_participant(participant)
    terminal_provider.bind(daemon, target["id"], command="vibe", terminal_id="%1")

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="before bind")
    assert exc.value.code == "transcript_untrusted"
    with pytest.raises(RemoteError, match="cwd/time"):
        await client.call("read_transcript", id=target["id"])

    rows = await client.call("transcript.candidates", id=target["id"])
    assert [row["location"] for row in rows["candidates"]] == [str(candidate.resolve())]

    await client.call(
        "transcript.bind",
        id=target["id"],
        candidate=str(candidate),
        confirm_id=target["id"],
    )
    job = await client.call("send", target=target["id"], prompt="after bind")
    history = await client.call("read_transcript", id=target["id"])

    assert job["state"] == "running"
    assert terminal_provider.deliveries == [("%1", "after bind")]
    assert history["path"] == str(candidate.resolve())
    assert any(event["text"] == "BOUND" for event in history["events"])


async def test_send_response_format_augments_prompt_and_survives_await_shape(
    client, terminal_provider, daemon
):
    target = await _target(client, daemon)
    serialized = '{"a":2,"b":1}'
    expected = _json_prompt(serialized, "do the thing")

    job = await client.call(
        "send",
        target=target["id"],
        prompt="do the thing",
        response_format={"b": 1, "a": 2},
    )

    binding = daemon.store.terminal_bindings.get(target["id"])
    assert terminal_provider.deliveries == [(binding.terminal_id, expected)]
    assert job["prompt"] == expected
    assert job["response_format"] == serialized
    assert job["structured_result"] is None
    assert job["structured_status"] is None

    daemon.jobs.finish(
        job["handle"],
        state=JobState.DONE,
        result='{"ok":true}',
        raw_result='{"ok":true}',
    )
    status = await client.call("jobs.status", handle=job["handle"])
    awaited = await client.call("jobs.await", handles=[job["handle"]], max_wait=1.0)

    for row in (status, awaited[0]):
        assert row["response_format"] == serialized
        assert row["structured_result"] == '{"ok":true}'
        assert row["structured_status"] == "parsed"


async def test_send_response_format_rejects_non_object_before_delivery(client, terminal_provider):
    target = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "send",
            target=target["id"],
            prompt="do the thing",
            response_format="json",
        )

    assert exc.value.code == "bad_request"
    assert "response_format must be a JSON object or null" in str(exc.value)
    assert terminal_provider.deliveries == []


async def test_send_to_unaddressable_rejected(client, terminal_provider):
    """send to an External (no pane) participant is rejected."""
    ext = await client.call("hello", harness="vibe", cwd="/tmp")  # no pane = external
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=ext["id"], prompt="hi")
    assert exc.value.code == "not_addressable"


async def test_send_to_busy_target_rejected(client, terminal_provider, daemon):
    """send to a target that already has a running send job is rejected."""
    target = await _target(client, daemon)
    # First send succeeds
    await client.call("send", target=target["id"], prompt="first")
    # Second send to the same target is rejected
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="second")
    assert exc.value.code == "busy"
    # Only the first prompt was delivered
    assert len(terminal_provider.deliveries) == 1
    binding = daemon.store.terminal_bindings.get(target["id"])
    assert terminal_provider.deliveries[0] == (binding.terminal_id, "first")


async def test_send_to_working_target_without_a_job_is_rejected(client, terminal_provider, daemon):
    target = await _target(client, daemon)
    daemon.registry.set_status(target["id"], Status.WORKING)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="replacement")

    assert exc.value.code == "busy"
    assert "not delivering now" in exc.value.message
    assert terminal_provider.deliveries == []
    assert daemon.store.running_jobs_for_target(target["id"]) == []


async def test_send_allowed_after_job_exceeds_ttl(client, terminal_provider, daemon, monkeypatch):
    """A replacement closes an expired prompt job before reserving itself.

    The prompt may never have reached the agent (a human cleared the
    composer), and the rescue timer cannot fire on an active participant.
    Past the TTL a replacement can proceed, but the predecessor must be
    terminal so FIFO completion cannot consume it.
    """
    import theater.daemon.rpc.sending as sending_mod
    from theater.constants.daemon import SEND_SUPERSEDED_ERROR_CODE
    from theater.daemon.rpc.sending import SEND_CLAIM_TTL

    target = await _target(client, daemon)
    job1 = await client.call("send", target=target["id"], prompt="first")

    # Drive the clock: the send handler computes `stale = now() - SEND_CLAIM_TTL`
    # and rejects jobs whose created_at is newer than that. By advancing `now`
    # past the TTL, the job's real created_at falls on the stale side and the
    # busy gate drops it.
    real_now = sending_mod.now()
    monkeypatch.setattr(sending_mod, "now", lambda: real_now + SEND_CLAIM_TTL + 1)

    # The stale job is closed before the new one reserves the pane.
    job2 = await client.call("send", target=target["id"], prompt="second")
    assert job2["state"] == "running"
    predecessor = await client.call("jobs.status", handle=job1["handle"])
    awaited = await client.call("jobs.await", handles=[job1["handle"]], max_wait=1.0)
    assert predecessor["state"] == "crashed"
    assert predecessor["error_code"] == SEND_SUPERSEDED_ERROR_CODE
    assert "superseded" in predecessor["result"].lower()
    assert "newer send handle" in predecessor["error"]
    assert awaited[0]["error_code"] == SEND_SUPERSEDED_ERROR_CODE
    assert "newer send handle" in awaited[0]["error"]
    assert [job.handle for job in daemon.store.running_jobs_for_target(target["id"])] == [
        job2["handle"]
    ]
    assert len(terminal_provider.deliveries) == 2
    binding = daemon.store.terminal_bindings.get(target["id"])
    assert terminal_provider.deliveries[1] == (binding.terminal_id, "second")


async def test_send_ttl_reads_compatibility_facade(client, terminal_provider, daemon, monkeypatch):
    import theater.daemon.rpc.sending as sending_mod
    from theater.constants.daemon import SEND_SUPERSEDED_ERROR_CODE
    from theater.daemon import methods

    target = await _target(client, daemon)
    old = await client.call("send", target=target["id"], prompt="first")

    real_now = sending_mod.now()
    monkeypatch.setattr(methods, "SEND_CLAIM_TTL", 0.1)
    monkeypatch.setattr(sending_mod, "now", lambda: real_now + 1.0)

    job = await client.call("send", target=target["id"], prompt="second")
    assert job["state"] == "running"
    assert daemon.jobs.get(old["handle"]).error_code == SEND_SUPERSEDED_ERROR_CODE


async def test_send_ttl_supersedes_every_expired_prompt_job(
    client, terminal_provider, daemon, monkeypatch
):
    import theater.daemon.rpc.sending as sending_mod
    from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS, SEND_SUPERSEDED_ERROR_CODE

    target = await _target(client, daemon)
    for handle in ("expired-1", "expired-2"):
        daemon.jobs.create(
            handle=handle,
            caller_id="cli",
            target_id=target["id"],
            kind="send",
            prompt=handle,
            cwd="/tmp",
        )

    real_now = sending_mod.now()
    monkeypatch.setattr(sending_mod, "now", lambda: real_now + SEND_CLAIM_TTL_SECONDS + 1)

    successor = await client.call("send", target=target["id"], prompt="successor")

    for handle in ("expired-1", "expired-2"):
        job = daemon.jobs.get(handle)
        assert job is not None
        assert job.state == JobState.CRASHED
        assert job.error_code == SEND_SUPERSEDED_ERROR_CODE
    assert [job.handle for job in daemon.store.running_jobs_for_target(target["id"])] == [
        successor["handle"]
    ]


async def test_send_ttl_supersedes_expired_job_before_refusing_fresh_job(
    client, terminal_provider, daemon, monkeypatch
):
    import theater.daemon.jobs as jobs_mod
    import theater.daemon.rpc.sending as sending_mod
    from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS, SEND_SUPERSEDED_ERROR_CODE

    target = await _target(client, daemon)
    expired = daemon.jobs.create(
        handle="expired",
        caller_id="cli",
        target_id=target["id"],
        kind="send",
        prompt="expired",
        cwd="/tmp",
    )
    current = sending_mod.now() + SEND_CLAIM_TTL_SECONDS + 1
    monkeypatch.setattr(jobs_mod, "now", lambda: current)
    fresh = daemon.jobs.create(
        handle="fresh",
        caller_id="cli",
        target_id=target["id"],
        kind="send",
        prompt="fresh",
        cwd="/tmp",
    )
    monkeypatch.setattr(sending_mod, "now", lambda: current)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="another")

    assert exc.value.code == "busy"
    assert terminal_provider.deliveries == []
    expired_after = daemon.jobs.get(expired.handle)
    assert expired_after.state == JobState.CRASHED
    assert expired_after.error_code == SEND_SUPERSEDED_ERROR_CODE
    assert daemon.jobs.get(fresh.handle).state == JobState.RUNNING
    rows = daemon.store.conn.execute(
        jobs.select().where(jobs.c.target_id == target["id"])
    ).fetchall()
    assert {row._mapping["handle"] for row in rows} == {expired.handle, fresh.handle}
    assert [job.handle for job in daemon.store.running_jobs_for_target(target["id"])] == [
        fresh.handle
    ]


async def test_send_ttl_leaves_expired_blank_prompt_job_running(
    client, terminal_provider, daemon, monkeypatch
):
    import theater.daemon.rpc.sending as sending_mod
    from theater.daemon.rpc.sending import SEND_CLAIM_TTL

    target = await _target(client, daemon)
    blank = daemon.jobs.create(
        handle="blank",
        caller_id="cli",
        target_id=target["id"],
        kind="send",
        prompt="",
        cwd="/tmp",
    )
    real_now = sending_mod.now()
    monkeypatch.setattr(sending_mod, "now", lambda: real_now + SEND_CLAIM_TTL + 1)

    successor = await client.call("send", target=target["id"], prompt="successor")

    assert daemon.jobs.get(blank.handle).state == JobState.RUNNING
    assert daemon.jobs.get(successor["handle"]).state == JobState.RUNNING


async def test_send_then_await_result(client, terminal_provider, daemon):
    """send → await → result: the full live-delivery loop."""
    target = await _target(client, daemon)
    job = await client.call("send", target=target["id"], prompt="what is 2+2")
    handle = job["handle"]

    # The observer detects turn-end and finishes the job.
    daemon.jobs.finish(handle, state=JobState.DONE, result="4")

    jobs = await client.call("jobs.await", handles=[handle], max_wait=2.0)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "done"
    assert jobs[0]["result"] == "4"


async def test_send_after_job_finishes_allows_resend(client, terminal_provider, daemon):
    """After a send job finishes, a new send to the same target works."""
    target = await _target(client, daemon)
    job1 = await client.call("send", target=target["id"], prompt="first")
    daemon.jobs.finish(job1["handle"], state=JobState.DONE, result="done")

    # Second send should succeed now
    job2 = await client.call("send", target=target["id"], prompt="second")
    assert job2["state"] == "running"
    assert len(terminal_provider.deliveries) == 2


async def test_a_bare_spawn_leaves_nothing_running(client, terminal_provider, daemon):
    """A spawn with no prompt asked nothing, so its job is already finished.

    Left running it would block every later send as busy, and eat the first
    turn end the human produces.
    """
    child = await client.call("spawn", harness="vibe", prompt="", approval="manual", cwd="/tmp")
    assert daemon.jobs.get(child["handle"]).state == JobState.DONE
    job = await client.call("send", target=child["id"], prompt="now do something")
    assert job["state"] == "running"


async def test_a_spawn_that_asked_for_something_is_still_pending(client, terminal_provider, daemon):
    """The counterpart: a spawn prompt occupies the pane like any other."""
    child = await client.call(
        "spawn", harness="vibe", prompt="do the thing", approval="manual", cwd="/tmp"
    )
    assert daemon.jobs.get(child["handle"]).state == JobState.RUNNING
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=child["id"], prompt="and another thing")
    assert exc.value.code == "busy"


async def test_send_bus_event(client, terminal_provider, daemon):
    """send creates an agent.send bus event."""
    target = await _target(client, daemon)
    await client.call("send", target=target["id"], prompt="hi there")

    events = await client.call("bus.tail", limit=100)
    kinds = [e["kind"] for e in events]
    assert kinds.count("agent.send") == 1
    send_event = next(e for e in events if e["kind"] == "agent.send")
    assert send_event["to_id"] == target["id"]
    assert "hi there" in send_event["payload"]["prompt"]


async def test_unknown_provider_evidence_blocks_send(client, terminal_provider, daemon):
    target = await _target(client, daemon)
    daemon.presence = UnknownPresence()
    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="go ahead")
    assert exc.value.code == "human_present"
    assert terminal_provider.deliveries == []


# ---- native routing (Wave 4A) ---------------------------------------------
#
# A participant whose harness has a live runtime is delivered through the
# control service instead of its provider terminal.


async def _install_native_runtime(daemon, pid: str):
    from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
    from theater.harness.contracts.runtime import RuntimeContext

    state = FakeRuntimeState(participant_id=pid, backend_generation=1)
    state.native_session_id = "thread-1"
    context = RuntimeContext(
        participant_id=pid, cwd="/tmp", io=FakeRuntimeIO(state), backend_generation=1
    )
    runtime = FakeRuntime(context)

    async def create():
        return runtime

    await daemon.runtime_manager.get_or_create(pid, backend_generation=1, create=create)
    return state


async def test_send_to_a_native_participant_delivers_through_the_runtime(
    client, daemon, terminal_provider
):
    target = await _target(client, daemon)
    state = await _install_native_runtime(daemon, target["id"])

    job = await client.call("send", target=target["id"], prompt="native prompt")

    assert job["state"] == "running"
    assert job["kind"] == "send"
    assert state.sent == ["native prompt"], "the runtime received the prompt"
    assert terminal_provider.deliveries == [], "no pane delivery for native wiring"
    assert state.native_turn_id is not None, "the receipt's turn was recorded"
    (event,) = [row for row in daemon.store.bus_tail() if row["kind"] == "agent.send"]
    assert event["to_id"] == target["id"]
    assert event["payload"] == {"handle": job["handle"], "prompt": "native prompt"}


async def test_second_native_send_while_busy_is_refused_and_counted(
    client, daemon, terminal_provider
):
    target = await _target(client, daemon)
    state = await _install_native_runtime(daemon, target["id"])

    await client.call("send", target=target["id"], prompt="first")
    with pytest.raises(RemoteError) as busy:
        await client.call("send", target=target["id"], prompt="second")
    assert busy.value.code == "busy"
    assert state.sent == ["first"], "nothing was re-delivered after the refusal"
    assert daemon.store.refusal_counts().get("busy") == 1


async def test_send_to_a_disconnected_native_participant_fails_closed(
    client, daemon, terminal_provider
):
    """A persisted native binding without a live runtime never falls back to tmux.

    The handler only routes through the control service; the service owns the
    fail-closed refusal, so its refusal is also what the refusal counter sees.
    """
    from theater.daemon.persistence.repositories.runtime_bindings import (
        ParticipantRuntimeBinding,
    )
    from theater.harness.contracts.runtime import RuntimeLifecyclePhase, RuntimeWiring

    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id, terminal_id="%4")
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=child.id,
            harness="vibe",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.DETACHED,
            native_session_id="thread-gone",
        )
    )
    sent_before = list(terminal_provider.deliveries)

    with pytest.raises(RemoteError) as raised:
        await client.call("send", target=child.id, prompt="no fallback", caller_id=parent.id)

    assert raised.value.code == "stale_target"
    assert "natively wired but its runtime is not connected" in raised.value.message
    assert "the send is refused and never falls back" in raised.value.message
    assert terminal_provider.deliveries == sent_before, "no pane text as a fallback"
    assert daemon.store.running_jobs_for_target(child.id) == [], "no job was created"
    assert daemon.store.refusal_counts().get("stale_target") == 1
