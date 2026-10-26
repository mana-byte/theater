"""Control RPCs that change a participant: steer, queue, settings, interrupt, if_busy."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.control_service import operation_rows
from tests.support.fake_runtime import FakeRuntime
from tests.support.native_rpc import (
    GUIDANCE,
    active_send,
    install_runtime,
    interrupt_events,
    native_pair,
    pair,
)
from tests.support.waiting import until, wait_until
from theater.daemon.controls import redirect
from theater.daemon.rails import ModelNotAllowed
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlKind,
    RuntimeCapability,
)
from theater.models import Status
from theater.protocol import RemoteError

pytestmark = pytest.mark.usefixtures("absent_presence")


class _ExplodingSteerRuntime(FakeRuntime):
    """A runtime whose steering I/O fails after the send was accepted."""

    async def steer(self, **kwargs):
        raise ConnectionError("runtime I/O exploded")


async def test_steer_amends_the_current_job_for_the_direct_parent(client, daemon):
    parent, child, state = await native_pair(daemon)
    job = await active_send(client, parent, child)

    steered = await client.call(
        "participant.steer",
        target=child.id,
        prompt="actually, also add tests",
        caller_id=parent.id,
    )

    assert isinstance(steered, dict)
    assert steered["handle"] == job["handle"], "steering never creates a replacement job"
    assert steered["prompt"] == "do the thing", "the original prompt contract is preserved"
    assert steered["state"] == "running"
    assert state.steered == [(state.native_turn_id, "actually, also add tests")]

    # Additive flat receipt: a régie caller can tell this amendment was
    # confirmed, not left unknown.
    assert steered["delivery"] == "accepted"
    assert steered["phase"] == "settled"
    assert steered["operation_id"]
    assert "reason" not in steered and "detail" not in steered
    event = next(row for row in daemon.store.bus_tail() if row["kind"] == "agent.steer")
    assert (event["from_id"], event["to_id"]) == (parent.id, child.id)
    assert event["payload"] == {
        "handle": job["handle"],
        "prompt": "actually, also add tests",
        "delivery": "accepted",
    }


async def test_steer_reports_an_unknown_delivery_instead_of_faking_success(client, daemon):
    """A runtime steer exception leaves the job running and the truth visible."""
    parent, child = pair(daemon)
    state = await install_runtime(daemon, child.id, cls=_ExplodingSteerRuntime)
    job = await active_send(client, parent, child)
    turn_before = state.native_turn_id

    steered = await client.call(
        "participant.steer", target=child.id, prompt="amend anyway", caller_id=parent.id
    )

    assert isinstance(steered, dict)
    assert steered["handle"] == job["handle"]
    assert steered["state"] == "running", "the amended job keeps running"
    assert steered["prompt"] == "do the thing"
    assert steered["delivery"] == "unknown"
    assert steered["phase"] == "settled"
    assert steered["operation_id"]
    assert steered["reason"] == "delivery_unknown"
    assert "runtime I/O exploded" in steered["detail"]
    assert state.native_turn_id == turn_before, "the active turn is untouched"


async def test_steer_receipt_reports_the_latest_operation_for_the_job(client, daemon):
    """A second amendment on the same job reports its own just-created operation."""
    parent, child, _state = await native_pair(daemon)
    await active_send(client, parent, child)

    first = await client.call(
        "participant.steer", target=child.id, prompt="first amendment", caller_id=parent.id
    )
    second = await client.call(
        "participant.steer", target=child.id, prompt="second amendment", caller_id=parent.id
    )

    assert first["delivery"] == "accepted" and second["delivery"] == "accepted"
    assert second["operation_id"] != first["operation_id"], "the latest entry is selected"
    operations = daemon.store.control_operations_for_job(second["handle"])
    assert second["operation_id"] in {operation.operation_id for operation in operations}


async def test_steer_with_no_persisted_operation_reports_unknown_not_success(
    client, daemon, monkeypatch
):
    """An inexplicably missing operation is an honest unknown, never success."""
    parent, child, _state = await native_pair(daemon)
    await active_send(client, parent, child)
    monkeypatch.setattr(daemon.store, "control_operations_for_job", lambda job_handle: [])

    steered = await client.call(
        "participant.steer", target=child.id, prompt="amend", caller_id=parent.id
    )

    assert steered["delivery"] == "unknown"
    assert steered["phase"] is None
    assert steered["operation_id"] is None
    assert steered["reason"] == "steer_operation_missing"
    assert "could not be read back" in steered["detail"]
    assert "do not retry blindly" in steered["detail"]


async def test_steer_allows_the_local_operator_and_refuses_others(client, daemon):
    parent, child, state = await native_pair(daemon)
    await active_send(client, parent, child)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    by_cli = await client.call(
        "participant.steer", target=child.id, prompt="amend", caller_id="cli"
    )
    assert by_cli["state"] == "running"

    for caller_id, label in ((stranger.id, "stranger"), (child.id, "self")):
        with pytest.raises(RemoteError) as raised:
            await client.call(
                "participant.steer", target=child.id, prompt="amend", caller_id=caller_id
            )
        assert raised.value.code == "not_your_child", label
        if label == "stranger":
            assert "control-transfer" in raised.value.details["next"]
    assert len(state.steered) == 1, "refused steers must not reach the runtime"


async def test_steer_refuses_a_mismatched_job_handle(client, daemon):
    parent, child, _state = await native_pair(daemon)
    await active_send(client, parent, child)

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.steer",
            target=child.id,
            prompt="amend",
            caller_id=parent.id,
            job_handle="not-a-job",
        )
    assert raised.value.code == "stale_target"
    assert "if_busy" in raised.value.details["next"]


async def test_steer_on_legacy_wiring_names_the_alternatives(client, daemon):
    parent, child = pair(daemon)

    with pytest.raises(RemoteError) as raised:
        await client.call("participant.steer", target=child.id, prompt="amend", caller_id=parent.id)
    assert raised.value.code == "bad_request"
    assert "has not declared that Enter steers" in raised.value.message
    assert "queued as a followup" in raised.value.message


async def test_steer_validates_parameters_at_the_boundary(client, daemon):
    parent, child, _state = await native_pair(daemon)
    for bad_params, hint in (
        ({"target": child.id, "caller_id": parent.id}, "missing prompt"),
        ({"target": child.id, "prompt": 42, "caller_id": parent.id}, "non-string prompt"),
        ({"target": child.id, "prompt": "amend"}, "missing caller"),
        ({"prompt": "amend", "caller_id": parent.id}, "missing target"),
        ({"target": child.id, "prompt": "", "caller_id": parent.id}, "empty prompt"),
    ):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.steer", **bad_params)
        assert raised.value.code == "bad_request", hint
        assert "participant.steer" in raised.value.message


async def test_queue_followup_returns_a_new_awaitable_handle(client, daemon):
    parent, child, _state = await native_pair(daemon)
    job = await active_send(client, parent, child)

    queued = await client.call(
        "participant.queue_followup",
        target=child.id,
        prompt="and then the other thing",
        caller_id=parent.id,
    )

    assert isinstance(queued, dict)
    assert queued["handle"] != job["handle"], "a followup is a new job handle"
    assert queued["state"] == "running"
    assert queued["kind"] == "send"
    assert queued["target_id"] == child.id
    assert queued["caller_id"] == parent.id

    controls = await client.call("participant.controls", target=child.id)
    assert isinstance(controls, dict)
    assert controls["queued"] == [queued["handle"]], "the active send is not queued"
    event = next(row for row in daemon.store.bus_tail() if row["kind"] == "agent.queue_followup")
    assert (event["from_id"], event["to_id"]) == (parent.id, child.id)
    assert event["payload"] == {
        "handle": queued["handle"],
        "prompt": "and then the other thing",
    }


async def test_queue_followup_requires_the_direct_parent_or_operator(client, daemon):
    parent, child, _state = await native_pair(daemon)
    await active_send(client, parent, child)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    by_cli = await client.call(
        "participant.queue_followup", target=child.id, prompt="later", caller_id="cli"
    )
    assert by_cli["state"] == "running"

    for caller_id, label in ((stranger.id, "stranger"), (child.id, "self")):
        with pytest.raises(RemoteError) as raised:
            await client.call(
                "participant.queue_followup",
                target=child.id,
                prompt="later",
                caller_id=caller_id,
            )
        assert raised.value.code == "not_your_child", label


async def test_queue_followup_refuses_an_unbound_participant(client, daemon):
    parent, child = pair(daemon)
    daemon.registry.set_status(child.id, Status.WORKING)

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.queue_followup",
            target=child.id,
            prompt="followup without a terminal",
            caller_id=parent.id,
        )
    assert raised.value.code == "bad_request"
    assert daemon.store.running_jobs_for_target(child.id) == []


async def test_queue_followup_carries_response_format_guidance_exactly_once_native(client, daemon):
    """The queued native delivery carries the guidance once; the job keeps the schema."""
    parent, child, state = await native_pair(daemon)
    queued = await client.call(
        "participant.queue_followup",
        target=child.id,
        prompt="return structured json",
        response_format={"type": "object"},
        caller_id=parent.id,
    )

    assert queued["response_format"] == '{"type":"object"}'
    assert await until(lambda: state.sent), "the idle queue dispatches through the runtime"
    delivered = state.sent[0]
    assert delivered.count(GUIDANCE) == 1, "the guidance is injected exactly once"
    assert delivered.endswith("\n\nreturn structured json"), "the raw prompt is preserved"


async def test_queue_followup_without_response_format_delivers_the_prompt_byte_for_byte(
    client, daemon
):
    """No schema means the native delivery remains the raw prompt."""
    native_parent, native_child, state = await native_pair(daemon)
    native_queued = await client.call(
        "participant.queue_followup",
        target=native_child.id,
        prompt="plain native followup",
        caller_id=native_parent.id,
    )
    assert native_queued["response_format"] is None
    assert await until(lambda: state.sent), "the idle native queue dispatches"
    assert state.sent[0] == "plain native followup", "byte-for-byte, no guidance"
    native_job = daemon.store.get_job(native_queued["handle"])
    assert native_job.prompt == "plain native followup"


async def test_queue_followup_validates_parameters(client, daemon):
    parent, child, _state = await native_pair(daemon)
    for bad_params in (
        {"target": child.id, "caller_id": parent.id},
        {"target": child.id, "prompt": [], "caller_id": parent.id},
        {"target": child.id, "prompt": "x", "caller_id": 1},
    ):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.queue_followup", **bad_params)
        assert raised.value.code == "bad_request"


async def test_settings_update_applies_after_native_readback(client, daemon):
    parent, child, _state = await native_pair(daemon)
    daemon.config.models["vibe"] = ["gpt-5.6-sol"]
    daemon.config.reasoning["vibe"] = ["high"]

    outcome = await client.call(
        "participant.settings.update",
        target=child.id,
        caller_id=parent.id,
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )

    assert isinstance(outcome, dict)
    assert outcome["id"] == child.id
    assert outcome["applied"] is True
    assert outcome["model"] == "gpt-5.6-sol", "effective values only after readback"
    assert outcome["reasoning_effort"] == "high"
    assert "error" not in outcome and "error_code" not in outcome


async def test_settings_update_enforces_the_configured_allowlists(client, daemon):
    parent, child, _state = await native_pair(daemon)

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            model="gpt-5.6-sol",
        )
    assert raised.value.code == "model_not_allowed"
    assert operation_rows(daemon.store, child.id, ControlKind.SETTINGS_UPDATE) == []

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert raised.value.code == "reasoning_not_allowed"
    assert operation_rows(daemon.store, child.id, ControlKind.SETTINGS_UPDATE) == []


async def test_settings_service_enforces_participant_allowlist_without_a_wire_adapter(daemon):
    parent, child, state = await native_pair(daemon)

    with pytest.raises(ModelNotAllowed):
        await daemon.controls.update_settings(child.id, caller_id=parent.id, model="not-configured")

    assert state.settings == {}
    assert operation_rows(daemon.store, child.id, ControlKind.SETTINGS_UPDATE) == []


async def test_settings_update_requires_idle_and_authorization(client, daemon):
    parent, child, _state = await native_pair(daemon)
    daemon.config.reasoning["vibe"] = ["high"]
    await active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert busy.value.code == "busy"
    assert "await the child's running handle" in busy.value.details["next"]
    assert "update_session_settings" in busy.value.details["next"]

    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as unauthorized:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=stranger.id,
            reasoning_effort="high",
        )
    assert unauthorized.value.code == "not_your_child"


async def test_settings_update_reports_unavailable_capability_reasons(client, daemon):
    parent, child, state = await native_pair(daemon)
    daemon.config.reasoning["vibe"] = ["high"]
    state.unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert raised.value.code == "bad_request"
    assert "gated_by_backend" in raised.value.message


async def test_settings_update_on_legacy_wiring_is_refused(client, daemon):
    parent, child = pair(daemon)
    daemon.config.models["vibe"] = ["any"]
    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update", target=child.id, caller_id=parent.id, model="any"
        )
    assert raised.value.code == "bad_request"
    assert "fixed at launch" in raised.value.message


async def _working_child(daemon, terminal_provider):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)
    daemon.registry.set_status(child.id, Status.WORKING)
    return parent, daemon.registry.get(child.id)


async def test_interrupt_refuses_self_and_non_child_callers(client, daemon, terminal_provider):
    _parent, child = await _working_child(daemon, terminal_provider)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    for caller_id in (child.id, stranger.id):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=caller_id)
        assert raised.value.code == "not_your_child"

    assert interrupt_events(daemon) == []


async def _native_working_child(daemon, terminal_provider):
    from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
    from theater.daemon.persistence.repositories.runtime_bindings import (
        ParticipantRuntimeBinding,
    )
    from theater.harness.contracts.runtime import (
        RuntimeContext,
        RuntimeLifecyclePhase,
        RuntimeWiring,
    )
    from theater.models import now

    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    terminal_provider.bind(daemon, child.id)

    state = FakeRuntimeState(participant_id=child.id, backend_generation=1)
    state.native_session_id = "thread-1"
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=child.id,
            harness="vibe",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ACTIVE,
            native_session_id="thread-1",
            created_at=now(),
            updated_at=now(),
        )
    )
    context = RuntimeContext(
        participant_id=child.id, cwd="/tmp", io=FakeRuntimeIO(state), backend_generation=1
    )
    runtime = FakeRuntime(context)

    async def create():
        return runtime

    installed = await daemon.runtime_manager.get_or_create(
        child.id, backend_generation=1, create=create
    )
    assert daemon.runtime_manager.record_snapshot(child.id, installed, await installed.snapshot())
    return parent, daemon.registry.get(child.id), state


async def test_native_interrupt_cancels_queued_followups_and_requests_the_turn(
    client, daemon, terminal_provider
):
    parent, child, state = await _native_working_child(daemon, terminal_provider)
    active = await client.call("send", target=child.id, prompt="first", caller_id=parent.id)
    queued = await client.call(
        "participant.queue_followup", target=child.id, prompt="later", caller_id=parent.id
    )
    active_turn = state.native_turn_id
    assert active_turn is not None

    result = await client.call("participant.interrupt", target=child.id, caller_id=parent.id)

    assert result["id"] == child.id
    assert result["interrupted"] is True
    assert result["interrupted_job"] == active["handle"]
    assert result["cancelled_followups"] == [queued["handle"]]
    assert state.interrupted == [active_turn]
    assert daemon.store.get_job(queued["handle"]).state == "killed"
    assert daemon.store.get_job(active["handle"]).state == "running", (
        "the active job finishes only from terminal evidence"
    )


async def test_native_interrupt_while_idle_still_clears_the_queue(
    client, daemon, terminal_provider
):
    parent, child, state = await _native_working_child(daemon, terminal_provider)
    await client.call("send", target=child.id, prompt="first", caller_id=parent.id)
    queued = await client.call(
        "participant.queue_followup", target=child.id, prompt="later", caller_id=parent.id
    )
    # The turn ends without Theater involvement (the human let it finish in
    # the native UI); the queued followup has not dispatched yet.
    state.native_turn_id = None

    result = await client.call("participant.interrupt", target=child.id, caller_id=parent.id)

    assert result == {
        "id": child.id,
        "interrupted": False,
        "reason": "already_idle",
        "cancelled_followups": [queued["handle"]],
    }
    assert daemon.store.get_job(queued["handle"]).state == "killed"


async def test_native_interrupt_authorizes_like_the_existing_gate(
    client, daemon, terminal_provider
):
    _parent, child, _state = await _native_working_child(daemon, terminal_provider)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    for caller_id in (stranger.id, child.id):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.interrupt", target=child.id, caller_id=caller_id)
        assert raised.value.code == "not_your_child"
    assert interrupt_events(daemon) == []


async def _send(client, child, caller, mode, **extra):
    return await client.call(
        "send", target=child.id, prompt="new plan", caller_id=caller.id, if_busy=mode, **extra
    )


async def test_idle_target_gets_a_plain_send_and_strangers_are_refused(client, daemon):
    parent, child, _state = await native_pair(daemon)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    with pytest.raises(RemoteError) as raised:
        await _send(client, child, stranger, "queue")
    assert raised.value.code == "not_your_child"

    sent = await _send(client, child, parent, "replace")
    assert sent["route"] == "send"
    assert "Theater:" not in sent["prompt"], "an idle target gets no interruption card"


async def test_queue_and_amend_route_a_busy_native_child(client, daemon):
    parent, child, state = await native_pair(daemon)
    active = await active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await _send(client, child, parent, "refuse")
    assert busy.value.code == "busy" and "if_busy" in busy.value.details["next"]
    assert "omit if_busy to queue" in busy.value.details["next"]

    peer = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as peer_busy:
        await _send(client, child, peer, "refuse")
    assert "omit if_busy to queue" in peer_busy.value.details["next"]
    assert 'if_busy="queue"' not in peer_busy.value.details["next"]

    queued = await _send(client, child, parent, "queue")
    assert queued["route"] == "queued" and queued["handle"] != active["handle"]

    amended = await _send(client, child, parent, "amend", job_handle=active["handle"])
    assert amended["route"] == "steer" and amended["handle"] == active["handle"]
    assert state.steered[-1][1] == "new plan"

    with pytest.raises(RemoteError, match="cannot apply response_format"):
        await _send(client, child, parent, "amend", response_format={"$theater": "delegate"})
    assert state.steered[-1][1] == "new plan"

    with pytest.raises(RemoteError) as guarded:
        await _send(client, child, parent, "amend", job_handle=queued["handle"])
    assert guarded.value.code == "stale_target"


async def test_amend_without_a_steer_route_falls_back_to_the_queue(
    client, daemon, terminal_provider
):
    parent, child = pair(daemon)
    terminal_provider.bind(daemon, child.id)
    daemon.registry.set_status(child.id, Status.WORKING)

    amended = await _send(client, child, parent, "amend")

    assert amended["route"] == "queued"
    assert amended["steer_refused"] == "bad_request"


async def test_replace_interrupts_waits_for_idle_and_tells_the_child(client, daemon):
    parent, child, state = await native_pair(daemon)
    worker = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=child.id)
    launched = daemon.jobs.create(
        handle=worker.id, caller_id=child.id, target_id=worker.id, kind="spawn"
    )
    active = await active_send(client, parent, child)

    async def observer_finishes_the_interrupted_job():
        await wait_until(lambda: state.interrupted, what="interrupt observed")
        daemon.jobs.finish(active["handle"], state="killed", error_code="interrupted")

    finisher = asyncio.create_task(observer_finishes_the_interrupted_job())
    replaced = await _send(client, child, parent, "replace")
    await finisher

    assert replaced["route"] == "replace"
    assert replaced["interrupted_job"] == active["handle"]
    assert state.sent[-1].startswith(f"[Theater: your turn {active['handle']} was interrupted by ")
    assert f"{launched.handle} -> " in state.sent[-1]
    assert "await them, do not respawn.]\n\nnew plan" in state.sent[-1]

    status = await client.call("jobs.status", handle=active["handle"])
    assert status["replaced_by"] == replaced["handle"]
    assert "Re-send the prompt" in status["error"]


async def test_replace_times_out_without_sending(client, daemon, monkeypatch):
    parent, child, state = await native_pair(daemon)
    await active_send(client, parent, child)
    sent_before = list(state.sent)
    monkeypatch.setattr(redirect, "REPLACE_IDLE_WAIT_SECONDS", 0.3)

    with pytest.raises(RemoteError) as raised:
        await _send(client, child, parent, "replace")

    assert raised.value.code == "replace_timeout"
    assert state.interrupted, "the interrupt stays applied"
    assert state.sent == sent_before, "nothing was sent"
