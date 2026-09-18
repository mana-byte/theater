"""Focused control RPC tests: steer, queue, settings, controls.

The control service's own state machine is covered by ``test_control_service.py``;
these tests cover the daemon boundary: parameter validation, authorization
routing through the existing gate semantics, serialization, native routing,
and provider-only fallback policy. Native participants use the in-memory fake runtime
installed into the daemon's runtime manager — no backend is launched.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests._presence_doubles import AbsentPresence
from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import BUS_KIND_SEND_REFUSED
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.daemon.rpc.spawning import _wiring_param
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    NativeHumanInteraction,
    NativeInteractionKind,
    RuntimeCapability,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import Status
from theater.protocol import RemoteError

#: The exact response-format guidance prefix, injected exactly once.
_GUIDANCE = "Return your final answer as a single bare JSON value"


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    """No human focus: the composed gates pass for every test in this module."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


def _pair(daemon):
    """A parent and its unbound child (child has no runtime yet)."""
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    return parent, daemon.registry.get(child.id)


async def _install_runtime(daemon, pid: str, *, cls=FakeRuntime) -> FakeRuntimeState:
    state = FakeRuntimeState(participant_id=pid, backend_generation=1)
    state.native_session_id = "thread-1"
    context = RuntimeContext(
        participant_id=pid,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=1,
    )
    runtime = cls(context)

    async def create():
        return runtime

    installed = await daemon.runtime_manager.get_or_create(pid, backend_generation=1, create=create)
    assert installed is not None
    return state


async def _native_pair(daemon):
    parent, child = _pair(daemon)
    state = await _install_runtime(daemon, child.id)
    return parent, daemon.registry.get(child.id), state


async def test_native_route_drives_identity_and_metric_addressability(client, daemon):
    parent, child, _state = await _native_pair(daemon)

    identified = await client.call("hello", id=child.id, harness="vibe", cwd="/tmp")
    assert identified["addressable"] is True
    assert daemon.registry.addressable_count() == 1
    unbound = await client.call("participants.get", id=parent.id)
    assert unbound["addressable"] is False

    daemon.registry.mark_dead(child.id)
    rows = await client.call("participants.list", ids=[child.id], include_dead=True)
    assert rows[0]["addressable"] is False
    assert daemon.registry.addressable_count() == 0


def _disconnected_pair(daemon):
    """A native-wired child whose runtime is not connected (no fallback)."""
    parent, child = _pair(daemon)
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
    return parent, child


class _ExplodingSteerRuntime(FakeRuntime):
    """A runtime whose steering I/O fails after the send was accepted."""

    async def steer(self, **kwargs):
        raise ConnectionError("runtime I/O exploded")


async def _until(predicate, timeout: float = 5.0) -> bool:
    """Poll a predicate while the event loop runs (queue dispatch is a task)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


async def _active_send(client, parent, child) -> dict:
    """Ordinary native send through the existing ``send`` RPC."""
    job = await client.call("send", target=child.id, prompt="do the thing", caller_id=parent.id)
    assert isinstance(job, dict)
    return job


def _refusals(daemon) -> list[tuple[str, str]]:
    return [
        (event["from_id"], event["to_id"], event["payload"]["reason"])
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_SEND_REFUSED
    ]


# ---- steer ----------------------------------------------------------------


async def test_steer_amends_the_current_job_for_the_direct_parent(client, daemon):
    parent, child, state = await _native_pair(daemon)
    job = await _active_send(client, parent, child)

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
    parent, child = _pair(daemon)
    state = await _install_runtime(daemon, child.id, cls=_ExplodingSteerRuntime)
    job = await _active_send(client, parent, child)
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
    parent, child, _state = await _native_pair(daemon)
    await _active_send(client, parent, child)

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
    parent, child, _state = await _native_pair(daemon)
    await _active_send(client, parent, child)
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
    parent, child, state = await _native_pair(daemon)
    await _active_send(client, parent, child)
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
    assert len(state.steered) == 1, "refused steers must not reach the runtime"


async def test_steer_refuses_a_mismatched_job_handle(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    await _active_send(client, parent, child)

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.steer",
            target=child.id,
            prompt="amend",
            caller_id=parent.id,
            job_handle="not-a-job",
        )
    assert raised.value.code == "stale_target"


async def test_steer_on_legacy_wiring_names_the_alternatives(client, daemon):
    parent, child = _pair(daemon)

    with pytest.raises(RemoteError) as raised:
        await client.call("participant.steer", target=child.id, prompt="amend", caller_id=parent.id)
    assert raised.value.code == "bad_request"
    assert "no runtime" in raised.value.message
    assert "queued as a followup" in raised.value.message


async def test_steer_validates_parameters_at_the_boundary(client, daemon):
    parent, child, _state = await _native_pair(daemon)
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


# ---- queue_followup --------------------------------------------------------


async def test_queue_followup_returns_a_new_awaitable_handle(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    job = await _active_send(client, parent, child)

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
    parent, child, _state = await _native_pair(daemon)
    await _active_send(client, parent, child)
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
    parent, child = _pair(daemon)
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
    parent, child, state = await _native_pair(daemon)
    queued = await client.call(
        "participant.queue_followup",
        target=child.id,
        prompt="return structured json",
        response_format={"type": "object"},
        caller_id=parent.id,
    )

    assert queued["response_format"] == '{"type":"object"}'
    assert await _until(lambda: state.sent), "the idle queue dispatches through the runtime"
    delivered = state.sent[0]
    assert delivered.count(_GUIDANCE) == 1, "the guidance is injected exactly once"
    assert delivered.endswith("\n\nreturn structured json"), "the raw prompt is preserved"


async def test_queue_followup_without_response_format_delivers_the_prompt_byte_for_byte(
    client, daemon
):
    """No schema means the native delivery remains the raw prompt."""
    native_parent, native_child, state = await _native_pair(daemon)
    native_queued = await client.call(
        "participant.queue_followup",
        target=native_child.id,
        prompt="plain native followup",
        caller_id=native_parent.id,
    )
    assert native_queued["response_format"] is None
    assert await _until(lambda: state.sent), "the idle native queue dispatches"
    assert state.sent[0] == "plain native followup", "byte-for-byte, no guidance"
    native_job = daemon.store.get_job(native_queued["handle"])
    assert native_job.prompt == "plain native followup"


async def test_queue_followup_validates_parameters(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    for bad_params in (
        {"target": child.id, "caller_id": parent.id},
        {"target": child.id, "prompt": [], "caller_id": parent.id},
        {"target": child.id, "prompt": "x", "caller_id": 1},
    ):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.queue_followup", **bad_params)
        assert raised.value.code == "bad_request"


# ---- settings --------------------------------------------------------------


async def test_settings_update_applies_after_native_readback(client, daemon):
    parent, child, _state = await _native_pair(daemon)
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
    parent, child, _state = await _native_pair(daemon)

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            model="gpt-5.6-sol",
        )
    assert raised.value.code == "model_not_allowed"

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert raised.value.code == "reasoning_not_allowed"


async def test_settings_update_requires_idle_and_authorization(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    daemon.config.reasoning["vibe"] = ["high"]
    await _active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert busy.value.code == "busy"

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
    parent, child, state = await _native_pair(daemon)
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
    parent, child = _pair(daemon)
    daemon.config.models["vibe"] = ["any"]
    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update", target=child.id, caller_id=parent.id, model="any"
        )
    assert raised.value.code == "bad_request"
    assert "fixed at launch" in raised.value.message


# ---- controls --------------------------------------------------------------


async def test_controls_reports_the_native_snapshot(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    job = await _active_send(client, parent, child)

    controls = await client.call("participant.controls", target=child.name)

    assert controls["id"] == child.id
    assert controls["wiring"] == "native"
    assert controls["backend_generation"] == 1
    assert controls["native_session_id"] == "thread-1"
    assert controls["health"] == {"connection": "connected", "diagnostics": []}
    assert controls["settings"] == {"model": None, "reasoning_effort": None}
    assert controls["active_turn"]["job_handle"] == job["handle"]
    assert controls["active_turn"]["native_turn_id"] is not None
    assert controls["queued"] == []
    native_route = {
        "available": True,
        "transport": "native_runtime",
        "runtime_host": "detached_backend",
    }
    assert controls["capabilities"]["send"] == native_route
    assert controls["capabilities"]["steer"] == native_route
    assert controls["capabilities"]["settings_update"]["supported_fields"] == [
        "model",
        "reasoning_effort",
    ]


async def test_controls_distinguishes_a_human_only_turn(client, daemon):
    _parent, child, state = await _native_pair(daemon)
    state.native_turn_id = "turn-human"
    state.pending_interaction = NativeHumanInteraction(
        kind=NativeInteractionKind.APPROVAL,
        native_turn_id="turn-human",
        details="approve the tool call",
    )

    controls = await client.call("participant.controls", target=child.id)
    assert controls["active_turn"]["job_handle"] is None
    assert controls["active_turn"]["pending_interaction"] == {
        "kind": "approval",
        "native_turn_id": "turn-human",
        "details": "approve the tool call",
    }


async def test_controls_reports_unbound_capability_reasons(client, daemon):
    _parent, child = _pair(daemon)

    controls = await client.call("participant.controls", target=child.id)

    assert controls["wiring"] == "legacy"
    assert controls["health"] is None
    assert controls["settings"] is None
    assert controls["active_turn"] is None
    assert controls["capabilities"]["send"]["available"] is False
    assert controls["capabilities"]["send"]["transport"] is None
    assert controls["capabilities"]["queue_followup"] == controls["capabilities"]["send"]
    steer = controls["capabilities"]["steer"]
    assert steer == {
        "available": False,
        "reason": "wiring_mode",
        "detail": steer["detail"],
        "transport": None,
    }
    assert "does not support" in steer["detail"]
    assert controls["capabilities"]["settings_update"]["reason"] == "wiring_mode"
    assert "does not support" in controls["capabilities"]["settings_update"]["detail"]


async def test_controls_reports_a_disconnected_native_binding(client, daemon):
    _parent, child = _pair(daemon)
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

    controls = await client.call("participant.controls", target=child.id)
    assert controls["wiring"] == "native"
    assert controls["native_session_id"] == "thread-gone"
    assert controls["health"]["connection"] == str(ConnectionHealth.DISCONNECTED)
    assert controls["capabilities"]["send"]["available"] is False
    assert controls["capabilities"]["send"]["reason"] == "wiring_mode"


async def test_controls_reports_opencode_native_send_requires_connected_frontend(
    client, daemon, monkeypatch
):
    _parent, child = _pair(daemon)
    monkeypatch.setattr(
        "theater.daemon.controls.routing.get_harness",
        lambda name: SimpleNamespace(runtime=OPENCODE_MANIFEST.runtime),
    )
    daemon.store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id=child.id,
            harness="opencode",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            launch_policy=json.dumps({"runtime_host": "frontend"}),
        )
    )

    controls = await client.call("participant.controls", target=child.id)

    assert controls["wiring"] == "native"
    assert controls["capabilities"]["send"] == {
        "available": False,
        "reason": "wiring_mode",
        "detail": "the selected native runtime is not connected",
        "transport": "native_runtime",
        "runtime_host": "frontend",
    }
    assert controls["capabilities"]["queue_followup"] == controls["capabilities"]["send"]
    assert controls["capabilities"]["interrupt"]["available"] is False
    assert controls["capabilities"]["interrupt"]["transport"] is None
    assert controls["capabilities"]["steer"]["reason"] == "theater_policy"
    assert controls["capabilities"]["settings_update"]["reason"] == "theater_policy"

    with pytest.raises(RemoteError, match="natively wired"):
        await client.call("send", target=child.id, prompt="never fall back")


async def test_controls_reports_the_effective_queue_capability_from_send(client, daemon):
    """Theater's queue is FIFO and dispatches through SEND, so that is the truth.

    A Codex-like runtime marks its own native queue primitive unavailable
    (``theater_policy``); Theater still queues followups, so the public
    capability follows SEND — available while SEND is, and unavailable with
    SEND's reason when it is not.
    """
    _parent, child, state = await _native_pair(daemon)
    state.unavailable[RuntimeCapability.QUEUE_FOLLOWUP] = CapabilityUnavailableReason.THEATER_POLICY

    controls = await client.call("participant.controls", target=child.id)
    assert controls["capabilities"]["queue_followup"] == {
        "available": True,
        "transport": "native_runtime",
        "runtime_host": "detached_backend",
    }

    state.unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.GATED_BY_BACKEND
    controls = await client.call("participant.controls", target=child.id)
    send_entry = controls["capabilities"]["send"]
    queue_entry = controls["capabilities"]["queue_followup"]
    assert send_entry["available"] is False
    assert send_entry["reason"] == "gated_by_backend"
    assert queue_entry["available"] is False
    assert queue_entry["reason"] == "gated_by_backend", "SEND's reason, not theater_policy"
    assert "dispatches through the native send capability" in queue_entry["detail"]


async def test_controls_requires_a_known_target(client, daemon):
    with pytest.raises(RemoteError) as raised:
        await client.call("participant.controls", target="nosuch")
    assert raised.value.code == "not_found"


# ---- a persisted native binding without a live runtime ----------------------


async def test_every_control_fails_closed_for_a_disconnected_native_participant(client, daemon):
    """No blind tmux fallback: the service refuses, and the handlers delegate."""
    parent, child = _disconnected_pair(daemon)
    daemon.config.reasoning["vibe"] = ["high"]

    with pytest.raises(RemoteError) as queued:
        await client.call(
            "participant.queue_followup", target=child.id, prompt="later", caller_id=parent.id
        )
    assert queued.value.code == "stale_target"
    assert "natively wired but its runtime is not connected" in queued.value.message
    assert "the queue_followup is refused and never falls back" in queued.value.message
    assert "never queued as legacy work" in queued.value.message
    assert daemon.store.running_jobs_for_target(child.id) == [], "no job was created"
    controls = await client.call("participant.controls", target=child.id)
    assert controls["queued"] == [], "no queue reservation was made"

    with pytest.raises(RemoteError) as steered:
        await client.call("participant.steer", target=child.id, prompt="amend", caller_id=parent.id)
    assert steered.value.code == "stale_target"
    assert "the steer is refused and never falls back" in steered.value.message

    with pytest.raises(RemoteError) as settings:
        await client.call(
            "participant.settings.update",
            target=child.id,
            caller_id=parent.id,
            reasoning_effort="high",
        )
    assert settings.value.code == "stale_target"
    assert "the settings update is refused and never falls back" in settings.value.message

    with pytest.raises(RemoteError) as interrupted:
        await client.call("participant.interrupt", target=child.id, caller_id=parent.id)
    assert interrupted.value.code == "stale_target"
    assert "the interrupt is refused and never falls back" in interrupted.value.message

    assert daemon.store.running_jobs_for_target(child.id) == []

    # Authorization runs before classification inside the service: an
    # unauthorized caller learns nothing about the wiring and mutates nothing.
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as refused:
        await client.call(
            "participant.queue_followup", target=child.id, prompt="later", caller_id=stranger.id
        )
    assert refused.value.code == "not_your_child"
    assert daemon.store.running_jobs_for_target(child.id) == []


async def test_unbound_participants_have_no_terminal_fallback(client, daemon):
    parent, child = _pair(daemon)
    with pytest.raises(RemoteError) as raised:
        await client.call("send", target=child.id, prompt="no route", caller_id=parent.id)
    assert raised.value.code == "not_addressable"


# ---- native send/interrupt routing through the existing RPCs ----------------


async def test_send_routes_native_participants_through_the_control_service(client, daemon):
    parent, child, state = await _native_pair(daemon)
    job = await client.call("send", target=child.id, prompt="native hello", caller_id=parent.id)

    assert isinstance(job, dict)
    assert job["state"] == "running"
    assert state.sent == ["native hello"], "the prompt reached the runtime"


async def test_send_records_native_refusals_and_refuses_unbound_delivery(client, daemon):
    parent, child, _state = await _native_pair(daemon)
    await _active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await client.call("send", target=child.id, prompt="too soon", caller_id=parent.id)
    assert busy.value.code == "busy"
    assert (parent.id, child.id, "busy") in _refusals(daemon)

    other_parent, other_child = _pair(daemon)
    with pytest.raises(RemoteError) as unavailable:
        await client.call(
            "send", target=other_child.id, prompt="no route", caller_id=other_parent.id
        )
    assert unavailable.value.code == "not_addressable"


async def test_spawn_wiring_is_validated_at_the_boundary(client, daemon):
    with pytest.raises(RemoteError) as raised:
        await client.call(
            "spawn",
            harness="vibe",
            cwd="/tmp",
            approval="manual",
            wiring="bogus",
        )
    assert raised.value.code == "bad_request"
    assert "auto, native, or legacy" in raised.value.message

    with pytest.raises(RemoteError) as raised:
        await client.call(
            "spawn",
            harness="vibe",
            cwd="/tmp",
            approval="manual",
            wiring=42,
        )
    assert raised.value.code == "bad_request"


def test_wiring_param_defaults_to_auto_and_accepts_explicit_values():
    assert _wiring_param({}) is RuntimeWiring.AUTO
    assert _wiring_param({"wiring": None}) is RuntimeWiring.AUTO
    assert _wiring_param({"wiring": "auto"}) is RuntimeWiring.AUTO
    assert _wiring_param({"wiring": "legacy"}) is RuntimeWiring.LEGACY
    assert _wiring_param({"wiring": "native"}) is RuntimeWiring.NATIVE
