"""Focused control RPC tests: steer, queue, settings, controls.

The control service's own state machine is covered by ``test_control_service.py``;
these tests cover the daemon boundary: parameter validation, authorization
routing through the existing gate semantics, serialization, native routing,
and legacy compatibility. Native participants use the in-memory fake runtime
installed into the daemon's runtime manager — no backend is launched.
"""

from __future__ import annotations

import pytest

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import BUS_KIND_SEND_REFUSED
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
)
from theater.daemon.rpc.spawning import _wiring_param
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


def _pane(fake_tmux, *, pane: str = "%9", pid: int = 4242):
    fake_tmux.add_pane(pane, command="vibe", pid=pid)
    return pane, pid


def _pair(daemon, fake_tmux):
    """A parent and its pane-attached child (child has no runtime yet)."""
    fake_tmux.remove_pane("%9")
    pane, pid = _pane(fake_tmux)
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    daemon.registry.attach_pane(child.id, pane, pane_pid=pid)
    return parent, daemon.registry.get(child.id)


async def _install_runtime(daemon, pid: str) -> FakeRuntimeState:
    state = FakeRuntimeState(participant_id=pid, backend_generation=1)
    state.native_session_id = "thread-1"
    context = RuntimeContext(
        participant_id=pid,
        cwd="/tmp",
        io=FakeRuntimeIO(state),
        backend_generation=1,
    )
    runtime = FakeRuntime(context)

    async def create():
        return runtime

    installed = await daemon.runtime_manager.get_or_create(pid, backend_generation=1, create=create)
    assert installed is not None
    return state


async def _native_pair(daemon, fake_tmux):
    parent, child = _pair(daemon, fake_tmux)
    state = await _install_runtime(daemon, child.id)
    return parent, daemon.registry.get(child.id), state


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


async def test_steer_amends_the_current_job_for_the_direct_parent(client, daemon, fake_tmux):
    parent, child, state = await _native_pair(daemon, fake_tmux)
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


async def test_steer_allows_the_local_operator_and_refuses_others(client, daemon, fake_tmux):
    parent, child, state = await _native_pair(daemon, fake_tmux)
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


async def test_steer_refuses_a_mismatched_job_handle(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_steer_on_legacy_wiring_names_the_alternatives(client, daemon, fake_tmux):
    parent, child = _pair(daemon, fake_tmux)

    with pytest.raises(RemoteError) as raised:
        await client.call("participant.steer", target=child.id, prompt="amend", caller_id=parent.id)
    assert raised.value.code == "bad_request"
    assert "no runtime" in raised.value.message
    assert "queued as a followup" in raised.value.message


async def test_steer_validates_parameters_at_the_boundary(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_queue_followup_returns_a_new_awaitable_handle(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_queue_followup_requires_the_direct_parent_or_operator(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_queue_followup_works_on_the_legacy_transport(client, daemon, fake_tmux):
    parent, child = _pair(daemon, fake_tmux)
    daemon.registry.set_status(child.id, Status.WORKING)

    queued = await client.call(
        "participant.queue_followup",
        target=child.id,
        prompt="followup for a pane participant",
        caller_id=parent.id,
    )
    assert queued["state"] == "running"

    controls = await client.call("participant.controls", target=child.id)
    assert controls["wiring"] == "legacy"
    assert controls["queued"] == [queued["handle"]]


async def test_queue_followup_validates_parameters(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
    for bad_params in (
        {"target": child.id, "caller_id": parent.id},
        {"target": child.id, "prompt": [], "caller_id": parent.id},
        {"target": child.id, "prompt": "x", "caller_id": 1},
    ):
        with pytest.raises(RemoteError) as raised:
            await client.call("participant.queue_followup", **bad_params)
        assert raised.value.code == "bad_request"


# ---- settings --------------------------------------------------------------


async def test_settings_update_applies_after_native_readback(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_settings_update_enforces_the_configured_allowlists(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)

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


async def test_settings_update_requires_idle_and_authorization(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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


async def test_settings_update_reports_unavailable_capability_reasons(client, daemon, fake_tmux):
    parent, child, state = await _native_pair(daemon, fake_tmux)
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


async def test_settings_update_on_legacy_wiring_is_refused(client, daemon, fake_tmux):
    parent, child = _pair(daemon, fake_tmux)
    daemon.config.models["vibe"] = ["any"]
    with pytest.raises(RemoteError) as raised:
        await client.call(
            "participant.settings.update", target=child.id, caller_id=parent.id, model="any"
        )
    assert raised.value.code == "bad_request"
    assert "fixed at launch" in raised.value.message


# ---- controls --------------------------------------------------------------


async def test_controls_reports_the_native_snapshot(client, daemon, fake_tmux):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
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
    assert controls["capabilities"]["send"] == {"available": True}
    assert controls["capabilities"]["steer"] == {"available": True}


async def test_controls_distinguishes_a_human_only_turn(client, daemon, fake_tmux):
    _parent, child, state = await _native_pair(daemon, fake_tmux)
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


async def test_controls_reports_legacy_capability_reasons(client, daemon, fake_tmux):
    _parent, child = _pair(daemon, fake_tmux)

    controls = await client.call("participant.controls", target=child.id)

    assert controls["wiring"] == "legacy"
    assert controls["health"] is None
    assert controls["settings"] is None
    assert controls["active_turn"] is None
    assert controls["capabilities"]["send"] == {"available": True}
    assert controls["capabilities"]["queue_followup"] == {"available": True}
    steer = controls["capabilities"]["steer"]
    assert steer == {
        "available": False,
        "reason": "wiring_mode",
        "detail": steer["detail"],
    }
    assert "no runtime" in steer["detail"]
    assert controls["capabilities"]["settings_update"]["reason"] == "wiring_mode"
    assert "fixed at launch" in controls["capabilities"]["settings_update"]["detail"]


async def test_controls_reports_a_disconnected_native_binding(client, daemon, fake_tmux):
    _parent, child = _pair(daemon, fake_tmux)
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


async def test_controls_requires_a_known_target(client, daemon, fake_tmux):
    with pytest.raises(RemoteError) as raised:
        await client.call("participant.controls", target="nosuch")
    assert raised.value.code == "not_found"


# ---- native send/interrupt routing through the existing RPCs ----------------


async def test_send_routes_native_participants_through_the_control_service(
    client, daemon, fake_tmux
):
    parent, child, state = await _native_pair(daemon, fake_tmux)
    sent_before = len(fake_tmux.sent)

    job = await client.call("send", target=child.id, prompt="native hello", caller_id=parent.id)

    assert isinstance(job, dict)
    assert job["state"] == "running"
    assert state.sent == ["native hello"], "the prompt reached the runtime"
    assert len(fake_tmux.sent) == sent_before, "no tmux delivery for native wiring"


async def test_send_records_native_refusals_and_preserves_legacy_delivery(
    client, daemon, fake_tmux
):
    parent, child, _state = await _native_pair(daemon, fake_tmux)
    await _active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await client.call("send", target=child.id, prompt="too soon", caller_id=parent.id)
    assert busy.value.code == "busy"
    assert (parent.id, child.id, "busy") in _refusals(daemon)

    # The legacy pane path is untouched: same pane fixture, no runtime.
    other_parent, other_child = _pair(daemon, fake_tmux)
    job = await client.call(
        "send", target=other_child.id, prompt="legacy hello", caller_id=other_parent.id
    )
    assert job["state"] == "running"
    assert (other_child.tmux_pane, "legacy hello") in fake_tmux.sent


async def test_spawn_wiring_is_validated_at_the_boundary(client, daemon, fake_tmux):
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
