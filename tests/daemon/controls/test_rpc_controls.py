"""Controls RPC and native routing: snapshots, disconnected bindings, wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.native_rpc import (
    active_send,
    native_pair,
    pair,
)
from theater.constants.daemon import BUS_KIND_SEND_REFUSED
from theater.daemon.frontend.control_handlers import controls_get
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
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import Status
from theater.protocol import RemoteError

pytestmark = pytest.mark.usefixtures("absent_presence")


@pytest.mark.parametrize("pending", [False, True])
async def test_await_input_includes_only_available_native_interaction(client, daemon, pending):
    parent, child, state = await native_pair(daemon)
    state.native_turn_id = "turn-1"
    job = daemon.jobs.create(handle=child.id, caller_id=parent.id, target_id=child.id, kind="spawn")
    if pending:
        state.pending_interaction = NativeHumanInteraction(
            kind=NativeInteractionKind.APPROVAL,
            native_turn_id="turn-1",
            details="Allow command execution?",
        )
    runtime = daemon.runtime_manager.get(child.id)
    daemon.runtime_manager.record_snapshot(child.id, runtime, await runtime.snapshot())
    daemon.registry.set_status(child.id, Status.AWAITING_INPUT)

    entries = await client.call("jobs.await", handles=[job.handle], max_wait=1)
    entry = entries[0]
    assert entry["await_reason"] == "awaiting_input"
    assert entry["participant_status"] == "awaiting_input"
    assert entry["state"] == "running"
    if pending:
        assert entry["pending_interaction"] == {
            "kind": "approval",
            "native_turn_id": "turn-1",
            "details": "Allow command execution?",
        }
    else:
        assert "pending_interaction" not in entry

    private = await client.call("participant.controls", target=child.id)
    public = await controls_get(daemon, None, {"participant_id": child.id})
    for report in (private, public):
        assert report["active_turn"]["native_turn_id"] == "turn-1"
        assert report["active_turn"].get("pending_interaction") == entry.get("pending_interaction")

    daemon.jobs.finish(job.handle, state="done")
    entries = await client.call("jobs.await", handles=[job.handle], max_wait=1)
    assert entries[0]["await_reason"] == "job_terminal"
    assert "pending_interaction" not in entries[0]


async def test_native_route_drives_identity_and_metric_addressability(client, daemon):
    parent, child, _state = await native_pair(daemon)

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
    parent, child = pair(daemon)
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


def _refusals(daemon) -> list[tuple[str, str]]:
    return [
        (event["from_id"], event["to_id"], event["payload"]["reason"])
        for event in daemon.store.bus_tail(limit=100)
        if event["kind"] == BUS_KIND_SEND_REFUSED
    ]


async def test_controls_reports_the_native_snapshot(client, daemon):
    parent, child, _state = await native_pair(daemon)
    job = await active_send(client, parent, child)

    controls = await client.call("participant.controls", target=child.name)

    assert list(controls) == [
        "id",
        "wiring",
        "backend_generation",
        "native_session_id",
        "health",
        "settings",
        "capabilities",
        "transcript_identity",
        "active_turn",
        "queued",
        "human_presence",
    ]
    assert controls["id"] == child.id
    assert controls["wiring"] == "native"
    assert controls["backend_generation"] == 1
    assert controls["native_session_id"] == "thread-1"
    assert controls["health"] == {"connection": "connected", "diagnostics": []}
    assert controls["settings"] == {
        "model": None,
        "reasoning_effort": None,
        "supported_fields": ["model", "reasoning_effort"],
    }
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


async def test_private_controls_refreshes_snapshot_and_frontend_reads_cache(client, daemon):
    _parent, child, state = await native_pair(daemon)
    state.settings["model"] = "new-model"

    before = await controls_get(daemon, None, {"participant_id": child.id})
    assert before["settings"] == {
        "model": None,
        "reasoning_effort": None,
        "supported_fields": ["model", "reasoning_effort"],
    }

    private = await client.call("participant.controls", target=child.id)
    assert private["settings"] == {
        "model": "new-model",
        "reasoning_effort": None,
        "supported_fields": ["model", "reasoning_effort"],
    }

    after = await controls_get(daemon, None, {"participant_id": child.id})
    assert after["settings"] == private["settings"]


async def test_controls_distinguishes_a_human_only_turn(client, daemon):
    _parent, child, state = await native_pair(daemon)
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
    _parent, child = pair(daemon)

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
    _parent, child = pair(daemon)
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
    _parent, child = pair(daemon)
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
    _parent, child, state = await native_pair(daemon)
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
    parent, child = pair(daemon)
    with pytest.raises(RemoteError) as raised:
        await client.call("send", target=child.id, prompt="no route", caller_id=parent.id)
    assert raised.value.code == "not_addressable"


async def test_send_routes_native_participants_through_the_control_service(client, daemon):
    parent, child, state = await native_pair(daemon)
    job = await client.call("send", target=child.id, prompt="native hello", caller_id=parent.id)

    assert isinstance(job, dict)
    assert job["state"] == "running"
    assert state.sent == ["native hello"], "the prompt reached the runtime"


async def test_send_records_native_refusals_and_refuses_unbound_delivery(client, daemon):
    parent, child, _state = await native_pair(daemon)
    await active_send(client, parent, child)

    with pytest.raises(RemoteError) as busy:
        await client.call(
            "send", target=child.id, prompt="too soon", caller_id=parent.id, if_busy="refuse"
        )
    assert busy.value.code == "busy"
    assert (parent.id, child.id, "busy") in _refusals(daemon)

    other_parent, other_child = pair(daemon)
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
