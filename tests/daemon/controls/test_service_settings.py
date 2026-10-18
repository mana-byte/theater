"""Control service settings and capability gates: idle-only updates and recorded refusal reasons."""

from __future__ import annotations

import json

import pytest

from tests.support.control_harness import (
    Harness,
    make_runtime,
    open_harness,
    state_of,
    wrap_runtime,
)
from tests.support.control_service import operation_rows
from tests.support.fake_runtime import FakeRuntime
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlKind,
    ControlReceipt,
    DeliveryResult,
    RuntimeCapability,
    RuntimeSettingField,
    SessionOpenMode,
)
from theater.models import (
    BadRequest,
    Busy,
)


async def test_settings_idle_only_supplied_fields(store: Store) -> None:
    """Idle settings update: only supplied fields, effective after readback."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    recorder = harness.gates_recorder

    outcome = await service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is True
    assert outcome.model == "m2"  # effective value from the confirmed readback
    assert outcome.reasoning_effort is None  # untouched field stays untouched
    assert state.settings == {"model": "m2"}
    assert recorder.settings_checks == [("m2", None)]
    # The settings operation row carries only the supplied fields — approval
    # and sandbox policy are immutable and have no representation here.
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["job_handle"] is None  # settings follow no Theater job
    assert json.loads(settings_row["payload"]) == {"model": "m2"}
    assert settings_row["delivery_result"] == str(DeliveryResult.ACCEPTED)


async def test_settings_rejects_busy_capability_and_empty_requests(store: Store) -> None:
    """Busy targets, gated capabilities, and empty requests all refuse."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    try:
        await service.update_settings("p1", caller_id="caller")
        raise AssertionError("an empty settings request must refuse")
    except BadRequest as exc:
        assert "nothing to update" in str(exc)

    state.unavailable = {
        RuntimeCapability.SETTINGS_UPDATE: CapabilityUnavailableReason.GATED_BY_BACKEND
    }
    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a gated capability must refuse")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE) == []
    state.unavailable = {}

    state.native_turn_id = "turn-active"
    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a busy target must refuse settings updates")
    except Busy as exc:
        assert "await the child's running handle" in exc.details["next"]
        assert "update_session_settings" in exc.details["next"]
    state.native_turn_id = None


async def test_settings_rejects_unsupported_fields_before_native_mutation(store: Store) -> None:
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    state.supported_settings = {RuntimeSettingField.REASONING_EFFORT}

    with pytest.raises(BadRequest, match=r"does not support updating settings field.*model"):
        await harness.service.update_settings("p1", caller_id="caller", model="m2")

    assert state.settings == {}
    assert operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE) == []


async def test_settings_uncertain_delivery_stays_visible(store: Store) -> None:
    """An uncertain settings application reports applied=None, never success."""

    class UncertainRuntime(FakeRuntime):
        async def update_settings(
            self,
            *,
            operation_id: str,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ControlReceipt:
            return ControlReceipt(operation_id=operation_id, result=DeliveryResult.UNKNOWN)

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), UncertainRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)

    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is None  # visibly uncertain
    assert outcome.model == "m2"
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["delivery_result"] == str(DeliveryResult.UNKNOWN)


async def test_capability_gates_refuse_with_recorded_reason(store: Store) -> None:
    """SEND/STEER/INTERRUPT fail closed with the recorded reason."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service

    # SEND refused, with the recorded reason, before anything is reserved.
    state.unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.GATED_BY_BACKEND
    try:
        await service.send("p1", caller_id="caller", prompt="nope")
        raise AssertionError("a gated send must be refused")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert operation_rows(store, "p1", ControlKind.SEND) == []
    # The queue is Theater-owned but its native delivery is a send: with SEND
    # unavailable the reservation is refused too, before the slot is spent.
    try:
        await service.queue_followup("p1", caller_id="caller", prompt="later")
        raise AssertionError("a queue reservation without SEND must be refused")
    except BadRequest as exc:
        assert "gated_by_backend" in str(exc)
    assert store.queued_control_operation_count("p1") == 0
    assert operation_rows(store, "p1", ControlKind.QUEUE_FOLLOWUP) == []
    state.unavailable.clear()

    job = await service.send("p1", caller_id="caller", prompt="active work")

    state.unavailable[RuntimeCapability.STEER] = (
        CapabilityUnavailableReason.UNSUPPORTED_NATIVE_VERSION
    )
    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("a gated steer must be refused")
    except BadRequest as exc:
        assert "unsupported_native_version" in str(exc)
    assert operation_rows(store, "p1", ControlKind.STEER) == []

    state.unavailable.clear()
    state.unavailable[RuntimeCapability.INTERRUPT] = CapabilityUnavailableReason.SESSION_STATE
    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("a gated interrupt must be refused")
    except BadRequest as exc:
        assert "session_state" in str(exc)
    assert operation_rows(store, "p1", ControlKind.INTERRUPT) == []
    state.unavailable.clear()
    del job


async def test_settings_readback_failure_reports_uncertain_application(store: Store) -> None:
    """An accepted settings receipt whose readback fails stays uncertain."""

    class ReadbackExplodingRuntime(FakeRuntime):
        """Accepts settings, then fails the effective-value readback."""

        def __init__(self, context) -> None:
            super().__init__(context)
            self.explode_snapshot = False

        async def update_settings(
            self,
            *,
            operation_id: str,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ControlReceipt:
            receipt = await super().update_settings(
                operation_id=operation_id, model=model, reasoning_effort=reasoning_effort
            )
            self.explode_snapshot = True
            return receipt

        async def snapshot(self):
            if self.explode_snapshot:
                raise RuntimeError("effective-value readback failed")
            return await super().snapshot()

    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), ReadbackExplodingRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)

    outcome = await harness.service.update_settings("p1", caller_id="caller", model="m2")

    assert outcome.applied is None  # explicitly uncertain, never success
    assert outcome.model == "m2"
    assert outcome.reasoning_effort is None
    assert outcome.error_code == "delivery_unknown"
    assert outcome.error is not None and "readback" in outcome.error
    # The delivery fact stands; the application is the uncertain part.
    (settings_row,) = operation_rows(store, "p1", ControlKind.SETTINGS_UPDATE)
    assert settings_row["delivery_result"] == str(DeliveryResult.ACCEPTED)
    assert harness.runtimes["p1"].state.settings == {"model": "m2"}
