"""Native route, capability and presence facts agree across public reads and admission."""

from __future__ import annotations

import pytest

from tests.support.builders import operator_context
from tests.support.control_service import operation_rows
from tests.support.presence import AbsentPresence, UnknownPresence
from tests.support.provider_controls import online, provider_target
from tests.support.provider_native import bind_native_participant
from tests.support.waiting import settle_operations
from theater.daemon.controls.routing import ControlRouteResolver
from theater.daemon.frontend.control_handlers import (
    controls_get,
    controls_send,
    controls_settings_update,
)
from theater.daemon.frontend.participant_read_handlers import participant_to_wire
from theater.daemon.harness_runtime.manager import CachedNativeRoute
from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeCapabilities,
    RuntimeCapability,
)
from theater.models import (
    JobState,
    PublicOperationState,
    StaleTarget,
    Tier,
    now,
)


async def _public_action_views(daemon, participant, *, actor: str):
    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    snapshot = daemon.state_service.snapshot(actor, page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    return participant_value, controls_value, snapshot_value


def test_native_route_is_not_replaced_by_terminal_binding(daemon) -> None:
    participant_id = provider_target(daemon)
    resolver = ControlRouteResolver(
        store=daemon.store,
        runtime_for=lambda _participant_id: object(),
        provider_health=lambda _provider_id, _generation: "online",
    )
    route = resolver.resolve(participant_id, RuntimeCapability.SEND)
    assert route.is_native
    assert not route.is_provider
    assert route.route_available
    assert route.native_facts is None


def test_native_control_route_reads_one_typed_cache_fact(daemon) -> None:
    participant_id = provider_target(daemon)
    facts = CachedNativeRoute(
        backend_generation=3,
        native_session_id="session-3",
        health=ConnectionHealth.DEGRADED,
        capabilities=RuntimeCapabilities(available=frozenset({RuntimeCapability.SEND})),
    )
    reads: list[tuple[str, object | None]] = []

    def native_facts(target_id: str, binding: object | None) -> CachedNativeRoute:
        reads.append((target_id, binding))
        return facts

    resolver = ControlRouteResolver(
        store=daemon.store,
        runtime_for=lambda _participant_id: object(),
        native_facts=native_facts,
    )

    route = resolver.resolve(participant_id, RuntimeCapability.SEND)

    assert reads == [(participant_id, None)]
    assert route.native_facts is facts
    assert route.native_capabilities is facts.capabilities
    assert route.route_available


def test_historical_pane_without_binding_is_not_a_control_route(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    participant.tier = Tier.SPAWNED
    participant.tmux_pane = "%legacy"
    daemon.store.upsert_participant(participant)
    resolver = ControlRouteResolver(store=daemon.store, runtime_for=lambda _participant_id: None)

    route = resolver.resolve(participant.id, RuntimeCapability.SEND)

    assert route.transport is None
    assert not route.route_available
    assert not route.is_provider
    assert not route.is_legacy


async def test_controls_projection_is_schema_valid_and_unknown_presence_blocks(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    daemon.presence = UnknownPresence()
    dispatched = False

    async def request(*_args):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("unknown presence must block provider delivery")

    monkeypatch.setattr(daemon.terminal_service.connections, "request", request)
    result = await controls_get(daemon, operator_context(), {"participant_id": participant_id})
    validator_for(METHOD_CATALOG["frontend.controls.get"].result_schema_id).validate(result)
    assert list(result) == [
        "actions",
        "revision",
        "transcript_identity",
        "id",
        "wiring",
        "backend_generation",
        "native_session_id",
        "health",
        "settings",
        "active_turn",
        "queued",
        "human_presence",
        "provider_id",
        "provider_generation",
        "terminal_id",
        "terminal_incarnation",
    ]
    assert result["actions"]["send"]["route_available"] is True
    assert result["actions"]["send"]["admissible"] is False
    assert result["actions"]["settings_update"]["supported"] is False
    assert result["wiring"] == "provider"
    assert result["health"] == {"connection": "online", "diagnostics": []}
    assert result["settings"] is None
    assert result["active_turn"] is None
    assert result["queued"] == []
    assert result["human_presence"]["state"] == "unknown"
    accepted = await controls_send(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "prompt": "blocked"},
        idempotency_key="provider-presence-blocked",
    )
    await settle_operations(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert control.delivery_phase is ControlDeliveryPhase.SETTLED
    assert control.delivery_result.value == "rejected"
    assert daemon.store.get_job(operation.job_handle).state == JobState.CRASHED
    assert not dispatched


async def test_presence_transition_publishes_one_controls_change_and_stable_poll_none(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    cursor = daemon.store.journal.current_sequence()

    await daemon.presence.refresh()

    changed = [
        event
        for group in daemon.store.journal.groups_after(cursor, limit=20)
        for event in group.events
        if event.kind == "participant.controls_changed" and event.entity_id == participant_id
    ]
    assert len(changed) == 1
    assert changed[0].payload["presence"] == "absent"

    cursor = daemon.store.journal.current_sequence()
    await daemon.presence.refresh()
    unchanged = [
        event
        for group in daemon.store.journal.groups_after(cursor, limit=20)
        for event in group.events
        if event.kind == "participant.controls_changed" and event.entity_id == participant_id
    ]
    assert unchanged == []


async def test_public_native_settings_reservation_precedes_detached_dispatch(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant, runtime, _state = await bind_native_participant(
        daemon, generation=7, session_id="native-session-settings"
    )
    participant_id = participant.id
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())
    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    accepted = await controls_settings_update(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="atomic-native-settings",
    )

    operation = daemon.operation_service.get(accepted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert operation.state == PublicOperationState.ACCEPTED.value
    assert control is not None
    assert control.transport is ControlTransport.NATIVE_RUNTIME
    assert control.backend_generation == 7
    assert control.native_session_id == "native-session-settings"
    assert started == [operation.operation_id]


async def test_public_settings_allowlist_refusal_keeps_accepted_reservation(daemon) -> None:
    participant, runtime, state = await bind_native_participant(
        daemon, generation=8, session_id="native-session-settings-refusal"
    )
    assert daemon.runtime_manager.record_snapshot(participant.id, runtime, await runtime.snapshot())

    accepted = await controls_settings_update(
        daemon,
        operator_context(),
        {"participant_id": participant.id, "model": "not-configured"},
        idempotency_key="native-settings-allowlist-refusal",
    )
    assert daemon.operation_service.get(accepted["operation_id"]).state == "accepted"

    await settle_operations(daemon)
    operation = daemon.operation_service.get(accepted["operation_id"])
    assert operation.state == PublicOperationState.FAILED.value
    assert operation.error is not None and operation.error["code"] == "model_not_allowed"
    assert len(operation_rows(daemon.store, participant.id, ControlKind.SETTINGS_UPDATE)) == 1
    assert state.settings == {}


async def test_cached_native_route_is_consistent_across_public_read_and_admission(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon.presence = AbsentPresence()
    participant, runtime, _state = await bind_native_participant(
        daemon, generation=9, session_id="native-session-consistent"
    )
    participant_id = participant.id

    unavailable = daemon.controls.route_for(participant_id, RuntimeCapability.SETTINGS_UPDATE)
    assert unavailable.route_available is False
    before, _cursor = daemon.store.operations.list_page(cursor=None, limit=100)
    with pytest.raises(StaleTarget, match=r"native runtime route.*unavailable"):
        await controls_settings_update(
            daemon,
            operator_context(),
            {"participant_id": participant_id, "model": "gpt-5"},
            idempotency_key="native-settings-before-snapshot",
        )
    after, _cursor = daemon.store.operations.list_page(cursor=None, limit=100)
    assert after == before

    participant_value = await participant_to_wire(daemon, participant)
    assert participant_value["native_route"]["health"] == "connected"
    assert participant_value["actions"]["settings_update"]["route_available"] is True
    cached = daemon.runtime_manager.cached_native_route(
        participant_id,
        backend_generation=9,
        native_session_id="native-session-consistent",
    )
    assert cached is not None and cached["health"] == "connected"
    assert daemon.controls.route_for(
        participant_id, RuntimeCapability.SETTINGS_UPDATE
    ).route_available

    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant_id}
    )
    assert controls_value["actions"]["settings_update"]["route_available"] is True
    snapshot = daemon.state_service.snapshot("native-route-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant_id
    )
    assert snapshot_value["native_route"] == participant_value["native_route"]
    assert snapshot_value["actions"]["settings_update"]["route_available"] is True

    started: list[str] = []

    def suppress_start(operation_id, *, dispatch, side_effect):
        del dispatch, side_effect
        started.append(operation_id)

    monkeypatch.setattr(daemon.operation_service, "start", suppress_start)
    admitted = await controls_settings_update(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="native-settings-after-snapshot",
    )
    assert started == [admitted["operation_id"]]
    control = daemon.store.get_control_operation(
        daemon.operation_service.get(admitted["operation_id"]).control_operation_id
    )
    assert control is not None
    assert (control.backend_generation, control.native_session_id) == (
        9,
        "native-session-consistent",
    )
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())


async def test_native_capability_projection_is_consistent_across_public_reads(daemon) -> None:
    daemon.presence = AbsentPresence()
    participant, _runtime, state = await bind_native_participant(
        daemon, generation=12, session_id="native-session-gated"
    )
    state.unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )

    before_controls = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    before_snapshot = daemon.state_service.snapshot("native-capability-before", page_size=500)
    before_value = next(
        item for item in before_snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert before_controls["actions"]["send"]["supported"] is False
    assert before_controls["actions"]["send"]["reason"] == "not_determined"
    assert before_controls["health"]["connection"] == "disconnected"
    assert before_controls["settings"] is None
    assert before_value["actions"]["send"]["supported"] is False
    assert before_value["actions"]["send"]["reason"] == "not_determined"

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    snapshot = daemon.state_service.snapshot("native-capability-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    expected = {
        "supported": False,
        "route_available": True,
        "admissible": False,
        "reason": "gated_by_backend",
        "detail": None,
    }
    assert participant_value["actions"]["settings_update"] == expected
    assert controls_value["actions"]["settings_update"] == expected
    assert snapshot_value["actions"]["settings_update"] == expected
    assert controls_value["health"] == {"connection": "connected", "diagnostics": []}
    assert controls_value["settings"] == {"model": None, "reasoning_effort": None}
    assert set(participant_value["native_route"]) == {
        "backend_generation",
        "native_session_id",
        "health",
    }


async def test_native_busy_admission_is_consistent_across_public_reads(daemon) -> None:
    daemon.presence = AbsentPresence()
    participant, _runtime, state = await bind_native_participant(
        daemon, generation=14, session_id="native-session-busy"
    )
    state.native_turn_id = "external-turn"

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    snapshot = daemon.state_service.snapshot("native-busy-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "busy"
        assert value["actions"]["settings_update"]["admissible"] is False
        assert value["actions"]["settings_update"]["reason"] == "busy"
        assert value["actions"]["steer"]["admissible"] is False
        assert value["actions"]["steer"]["reason"] == "stale_target"


async def test_native_settings_admission_requires_a_mutable_field(daemon) -> None:
    daemon.presence = AbsentPresence()
    participant, _runtime, state = await bind_native_participant(
        daemon, generation=15, session_id="native-session-fixed-settings"
    )
    state.supported_settings.clear()

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    snapshot = daemon.state_service.snapshot("native-settings-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["settings_update"]["supported"] is True
        assert value["actions"]["settings_update"]["admissible"] is False
        assert value["actions"]["settings_update"]["reason"] == "unsupported"


async def test_unknown_presence_detail_is_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    daemon.presence = UnknownPresence()
    participant = daemon.registry.get(participant_id)

    participant_value = await participant_to_wire(daemon, participant)
    controls_value = await controls_get(
        daemon, operator_context(), {"participant_id": participant.id}
    )
    snapshot = daemon.state_service.snapshot("unknown-presence-client", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    for value in (participant_value, controls_value, snapshot_value):
        assert value["actions"]["send"]["reason"] == "presence_unknown"
        assert value["actions"]["send"]["detail"] == "test double: unknown focus"


async def test_transcript_trust_is_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    daemon.presence = AbsentPresence()
    participant = daemon.registry.get(participant_id)
    participant.tier = Tier.ADOPTED
    daemon.store.upsert_participant(participant)

    values = await _public_action_views(daemon, participant, actor="untrusted-transcript")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "transcript_untrusted"

    daemon.observer.mark_transcript_identity_lost(participant.id, "test identity loss")
    values = await _public_action_views(daemon, participant, actor="lost-transcript")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "transcript_identity_lost"


async def test_empty_setting_allowlists_are_consistent_across_public_reads(daemon) -> None:
    daemon.presence = AbsentPresence()
    daemon.config.models["codex"] = []
    daemon.config.reasoning["codex"] = []
    participant, *_ = await bind_native_participant(
        daemon, generation=16, session_id="native-session-no-allowed-settings"
    )

    values = await _public_action_views(daemon, participant, actor="empty-setting-allowlists")
    for value in values:
        action = value["actions"]["settings_update"]
        assert action["supported"] is True
        assert action["admissible"] is False
        assert action["reason"] == "unsupported"
        assert action["detail"] == (
            "no runtime-supported setting field has configured allowable values"
        )

    daemon.config.models["codex"] = ["gpt-5"]
    values = await _public_action_views(daemon, participant, actor="allowed-model-setting")
    for value in values:
        action = value["actions"]["settings_update"]
        assert action["admissible"] is True
        assert action["reason"] is None


async def test_presence_snapshot_failures_are_consistent_across_public_reads(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingPresence:
        def snapshot(self, _participant_id: str):
            raise RuntimeError("provider read failed")

    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
    daemon.presence = FailingPresence()
    participant = daemon.registry.get(participant_id)

    values = await _public_action_views(daemon, participant, actor="failed-presence")
    for value in values:
        assert value["actions"]["send"]["admissible"] is False
        assert value["actions"]["send"]["reason"] == "presence_unknown"
        assert value["actions"]["send"]["detail"] == "presence snapshot failed"


async def test_private_native_send_refuses_a_durable_session_mismatch(daemon) -> None:
    daemon.presence = AbsentPresence()
    participant, _runtime, state = await bind_native_participant(
        daemon,
        generation=13,
        session_id="different-live-session",
        bound_session_id="durable-session",
    )

    with pytest.raises(StaleTarget, match="exact native runtime route"):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="identity mismatch")

    assert state.sent == []
    assert daemon.store.running_jobs_for_target(participant.id) == []


async def test_queued_native_followup_never_crosses_its_reserved_session(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon.presence = AbsentPresence()
    participant, _runtime, state = await bind_native_participant(
        daemon, generation=14, session_id="session-a"
    )
    monkeypatch.setattr(daemon.controls, "schedule_dispatch", lambda _participant_id: None)
    queued = await daemon.controls.queue_followup(
        participant.id,
        caller_id="cli",
        prompt="must stay in session A",
    )
    (reserved,) = daemon.store.queued_control_operations(participant.id)
    assert reserved.native_session_id == "session-a"

    assert daemon.store.bind_runtime_identity(
        participant.id,
        backend_generation=14,
        native_session_id="session-b",
        updated_at=now(),
    )
    state.native_session_id = "session-b"
    outcome = await daemon.controls.dispatch_queue(participant.id)

    assert outcome.failed == ((queued.handle, "stale_target"),)
    assert state.sent == []
    operation = daemon.store.get_control_operation(reserved.operation_id)
    assert operation is not None and operation.native_session_id == "session-a"


async def test_public_admission_fences_the_exact_cached_native_session(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon.presence = AbsentPresence()
    participant, runtime, state = await bind_native_participant(
        daemon, generation=10, session_id="native-session-a", bound_session_id=None
    )
    participant_id = participant.id
    assert daemon.runtime_manager.record_snapshot(participant_id, runtime, await runtime.snapshot())
    monkeypatch.setattr(
        daemon.operation_service,
        "start",
        lambda _operation_id, *, dispatch, side_effect: None,
    )

    with pytest.raises(StaleTarget, match=r"native runtime route.*unavailable"):
        await controls_settings_update(
            daemon,
            operator_context(),
            {"participant_id": participant_id, "model": "gpt-5"},
            idempotency_key="native-session-before-durable-bind",
        )
    assert (
        daemon.runtime_manager.cached_native_route(
            participant_id,
            backend_generation=10,
            native_session_id=None,
        )
        is None
    )

    assert daemon.store.bind_runtime_identity(
        participant_id,
        backend_generation=10,
        native_session_id="native-session-a",
        updated_at=now(),
    )
    admitted = await controls_settings_update(
        daemon,
        operator_context(),
        {"participant_id": participant_id, "model": "gpt-5"},
        idempotency_key="native-session-cache-fence",
    )
    operation = daemon.operation_service.get(admitted["operation_id"])
    control = daemon.store.get_control_operation(operation.control_operation_id)
    assert control is not None
    assert (control.backend_generation, control.native_session_id) == (
        10,
        "native-session-a",
    )

    state.native_session_id = "native-session-b"
    with pytest.raises(StaleTarget, match="native session changed"):
        daemon.controls._require_reserved_native_identity(control, await runtime.snapshot())


async def test_participant_read_rejects_a_cached_native_session_mismatch(daemon) -> None:
    daemon.presence = AbsentPresence()
    participant, *_ = await bind_native_participant(
        daemon, generation=11, session_id="different-session", bound_session_id="durable-session"
    )

    projected = await participant_to_wire(daemon, participant)
    assert projected["native_route"] == {
        "backend_generation": 11,
        "native_session_id": "durable-session",
        "health": "disconnected",
    }
    assert projected["actions"]["settings_update"]["route_available"] is False
    assert (
        daemon.controls.route_for(participant.id, RuntimeCapability.SETTINGS_UPDATE).route_available
        is False
    )
    snapshot = daemon.state_service.snapshot("mismatched-native-session", page_size=500)
    snapshot_value = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert snapshot_value["native_route"] == projected["native_route"]
    assert snapshot_value["actions"] == projected["actions"]
