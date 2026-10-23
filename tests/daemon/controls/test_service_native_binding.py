"""Control service native binding: spawn-job reuse and disconnected participants fail closed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.control_harness import (
    Harness,
    disconnected_native_harness,
    make_operation,
    open_harness,
    state_of,
)
from tests.support.control_service import native_outcome, operation_rows
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.persistence.store import Store
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    BadRequest,
    Job,
    JobState,
    NotAddressable,
    NotYourChild,
    StaleTarget,
    now,
)


def _spawn_job(
    harness: Harness,
    handle: str,
    *,
    target_id: str = "p1",
    caller_id: str = "caller",
    prompt: str = "initial prompt",
    response_format: str | None = None,
) -> Job:
    """The spawn job the native lifecycle creates before initial dispatch."""
    return harness.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="spawn",
        prompt=prompt,
        cwd=None,
        response_format=response_format,
    )


async def test_send_with_spawn_handle_reuses_the_spawn_job(store: Store) -> None:
    """The initial dispatch reuses the spawn job; no second job exists."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")

    job = await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")

    assert job.handle == "p1"  # the spawn job itself — nothing was reminted
    assert job.kind == "spawn"
    assert store.get_job("p1").state == JobState.RUNNING
    (send_op,) = store.control_operations_for_job("p1")
    assert send_op.kind is ControlKind.SEND
    assert send_op.job_handle == "p1"
    assert send_op.delivery_phase is ControlDeliveryPhase.SETTLED
    assert send_op.delivery_result is DeliveryResult.ACCEPTED
    assert state.sent == ["initial prompt"]
    # The reused spawn job is the only running job — and the active one.
    assert [j.handle for j in store.running_jobs_for_target("p1")] == ["p1"]
    assert [j.handle for j in service.active_jobs("p1")] == ["p1"]

    # Exact terminal evidence finishes the existing job, exactly once.
    finished = await service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=native_outcome(state)
    )
    assert finished is not None and finished.handle == "p1"
    assert finished.state == JobState.DONE
    assert [f for f in harness.jobs.finishes if f[0] == "p1"] == [("p1", "done")]


async def test_send_spawn_handle_mismatches_fail_closed(store: Store) -> None:
    """A wrong, terminal, or vanished handle refuses before transmission."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    spawn = _spawn_job(harness, "p1")

    async def refusal(**overrides) -> str:
        kwargs: dict = {
            "caller_id": "caller",
            "prompt": "initial prompt",
            "job_handle": "p1",
        }
        kwargs.update(overrides)
        try:
            await service.send("p1", **kwargs)
            raise AssertionError("a mismatched job handle must refuse")
        except BadRequest as exc:
            return str(exc)

    assert "does not exist" in await refusal(job_handle="p1#missing")
    plain = harness.jobs.create(
        handle="p1#plain",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="initial prompt",
        cwd=None,
    )
    assert "'send'" in await refusal(job_handle="p1#plain")  # not a spawn job
    other_target = _spawn_job(harness, "p2", target_id="p2")
    assert "another participant's" in await refusal(job_handle="p2")
    assert "caller contract" in await refusal(caller_id="someone-else")
    assert "spawn's prompt" in await refusal(prompt="a different prompt")
    assert "response-format contract" in await refusal(response_format="json")

    # Nothing was sent, reserved, or minted; every job is exactly as it was.
    assert state.sent == []
    assert operation_rows(store, "p1", ControlKind.SEND) == []
    assert store.get_job(spawn.handle).state == JobState.RUNNING
    assert store.get_job(plain.handle).state == JobState.RUNNING
    assert store.get_job(other_target.handle).state == JobState.RUNNING

    # A terminal spawn handle refuses too — after it, the job stays as finished.
    harness.jobs.finish(spawn.handle, state=JobState.DONE, result="done")
    assert "running spawn job" in await refusal()
    assert store.get_job(spawn.handle).state == JobState.DONE


async def test_send_spawn_handle_requires_an_available_route(store: Store) -> None:
    harness = Harness(store, {})

    with pytest.raises(NotAddressable, match="does not offer a transport"):
        await harness.service.send(
            "p1", caller_id="caller", prompt="initial prompt", job_handle="p1"
        )


async def test_persisted_native_binding_finishes_orphans_before_adoption(store: Store):
    """Restart classifies native-ness from the persisted binding, not the runtime registry."""
    harness = Harness(store, {})  # pre-adoption restart: no live runtimes
    native_spawn = harness.jobs.create(
        handle="p1",
        caller_id="caller",
        target_id="p1",
        kind="spawn",
        prompt="never dispatched",
        cwd=None,
    )
    native_send = harness.jobs.create(
        handle="p1#mid",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="reserved later",
        cwd=None,
    )
    legacy_spawn = harness.jobs.create(
        handle="p2",
        caller_id="caller",
        target_id="p2",
        kind="spawn",
        prompt="legacy delivery",
        cwd=None,
    )
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            created_at=now(),
            updated_at=now(),
        )
    )
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p2",
            harness="codex",
            wiring=RuntimeWiring.LEGACY,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.INTENDED,
            created_at=now(),
            updated_at=now(),
        )
    )

    failed = harness.service.fail_undelivered_followups(["p1", "p2"])

    assert {job.handle for job in failed} == {native_spawn.handle, native_send.handle}
    orphan = store.get_job(native_spawn.handle)
    assert orphan.state == JobState.CRASHED
    assert orphan.error_code == "daemon_restarted"
    assert "never replayed" in orphan.result
    assert store.get_job(native_send.handle).state == JobState.CRASHED
    legacy = store.get_job(legacy_spawn.handle)
    assert legacy.state == JobState.RUNNING  # the observer owns it


async def test_second_reuse_of_the_same_spawn_handle_refuses(store: Store) -> None:
    """One accepted initial dispatch; a second attempt never transmits again."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")

    first = await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
    assert first.handle == "p1"
    assert store.get_job("p1").state == JobState.RUNNING  # awaiting terminal evidence
    assert state.sent == ["initial prompt"]
    (op,) = store.control_operations_for_job("p1")

    try:
        await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
        raise AssertionError("a second initial dispatch of one spawn job must refuse")
    except BadRequest as exc:
        assert "exactly once" in str(exc)

    assert state.sent == ["initial prompt"]  # still exactly one transmission
    assert store.control_operations_for_job("p1") == [op]  # still exactly one operation
    assert store.get_job("p1").state == JobState.RUNNING  # untouched


async def test_reuse_refuses_a_spawn_job_with_a_reserved_operation(store: Store) -> None:
    """A crash-window RESERVED operation blocks a second attempt, any phase."""
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    service = harness.service
    _spawn_job(harness, "p1")
    store.reserve_control_operation(
        make_operation(
            "p1#reserved:send",
            job_handle="p1",
            kind=ControlKind.SEND,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.RESERVED,
            native_session_id=state.native_session_id,
        )
    )

    try:
        await service.send("p1", caller_id="caller", prompt="initial prompt", job_handle="p1")
        raise AssertionError("reuse of a spawn job with a reserved operation must refuse")
    except BadRequest as exc:
        assert "exactly once" in str(exc)

    assert state.sent == []  # nothing was transmitted
    (op,) = store.control_operations_for_job("p1")
    assert op.operation_id == "p1#reserved:send"  # the stranded row is untouched
    assert op.delivery_phase is ControlDeliveryPhase.RESERVED
    assert store.get_job("p1").state == JobState.RUNNING


def _no_operations_or_jobs(store: Store, participant_id: str) -> None:
    for kind in (
        ControlKind.SEND,
        ControlKind.STEER,
        ControlKind.QUEUE_FOLLOWUP,
        ControlKind.SETTINGS_UPDATE,
        ControlKind.INTERRUPT,
    ):
        assert operation_rows(store, participant_id, kind) == []
    assert store.running_jobs_for_target(participant_id) == []


async def test_disconnected_native_send_fails_closed_no_legacy_delivery(store: Store):
    """A persisted native binding with no runtime is not legacy: no pane send."""
    harness = disconnected_native_harness(store, "p1")

    try:
        await harness.service.send("p1", caller_id="caller", prompt="no fallback")
        raise AssertionError("a send to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "natively wired" in str(exc)
        assert "never falls back" in str(exc)

    assert ("p1", "caller", "send") in harness.gates_recorder.authorized
    _no_operations_or_jobs(store, "p1")


async def test_opencode_routes_supported_controls_native_and_never_falls_back(
    store: Store, monkeypatch
):
    monkeypatch.setattr(
        "theater.daemon.controls.routing.get_harness",
        lambda name: SimpleNamespace(runtime=OPENCODE_MANIFEST.runtime),
    )
    harness = Harness(store, {})
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="opencode",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=1,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            created_at=now(),
            updated_at=now(),
        )
    )

    assert harness.service.route_for("p1", RuntimeCapability.SEND).is_native
    assert harness.service.route_for("p1", RuntimeCapability.QUEUE_FOLLOWUP).is_native
    assert harness.service.route_for("p1", RuntimeCapability.INTERRUPT).transport is None
    assert harness.service.route_for("p1", RuntimeCapability.STEER).transport is None
    assert harness.service.route_for("p1", RuntimeCapability.SETTINGS_UPDATE).transport is None

    with pytest.raises(StaleTarget, match="natively wired"):
        await harness.service.send("p1", caller_id="caller", prompt="never fall back")
    _no_operations_or_jobs(store, "p1")

    with pytest.raises(BadRequest, match="unavailable on its selected transport"):
        await harness.service.steer("p1", caller_id="caller", prompt="never native")


async def test_disconnected_native_queue_fails_closed_no_reservation(store: Store):
    """A disconnected native never reserves a queue slot or creates a job."""
    harness = disconnected_native_harness(store, "p1")

    try:
        await harness.service.queue_followup("p1", caller_id="caller", prompt="never queued")
        raise AssertionError("a queue_followup to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "natively wired" in str(exc)

    assert store.queued_control_operation_count("p1") == 0
    _no_operations_or_jobs(store, "p1")


async def test_disconnected_native_steer_settings_interrupt_fail_closed(store: Store):
    """Steer, settings, and interrupt refuse with no mutation of any kind."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service

    try:
        await service.steer("p1", caller_id="caller", prompt="amend")
        raise AssertionError("a steer to a disconnected native must fail closed")
    except StaleTarget as exc:
        assert "never retried" in str(exc)

    try:
        await service.update_settings("p1", caller_id="caller", model="m2")
        raise AssertionError("a settings update on a disconnected native must fail closed")
    except StaleTarget:
        pass

    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("an interrupt of a disconnected native must fail closed")
    except StaleTarget:
        pass

    _no_operations_or_jobs(store, "p1")


async def test_disconnected_native_interrupt_leaves_the_queue_untouched(store: Store):
    """The interrupt refusal does not even cancel already-queued followups."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    # A followup queued while the runtime was connected; the runtime detached.
    harness.jobs.create(
        handle="p1#q",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="queued before the detach",
        cwd=None,
    )
    store.reserve_control_operation(
        make_operation(
            "p1#q:queue_followup",
            job_handle="p1#q",
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.QUEUED,
        )
    )

    try:
        await service.interrupt("p1", caller_id="caller")
        raise AssertionError("an interrupt of a disconnected native must fail closed")
    except StaleTarget:
        pass

    (queued_op,) = store.queued_control_operations("p1")  # nothing was cancelled
    assert queued_op.operation_id == "p1#q:queue_followup"
    assert queued_op.delivery_phase is ControlDeliveryPhase.QUEUED
    assert store.get_job("p1#q").state == JobState.RUNNING


async def test_disconnected_native_queue_dispatch_never_falls_back_to_the_pane(store: Store):
    """A scheduled dispatch pass defers a disconnected native's queue head."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    harness.jobs.create(
        handle="p1#q",
        caller_id="caller",
        target_id="p1",
        kind="send",
        prompt="waits for the runtime",
        cwd=None,
    )
    store.reserve_control_operation(
        make_operation(
            "p1#q:queue_followup",
            job_handle="p1#q",
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.NATIVE_RUNTIME,
            phase=ControlDeliveryPhase.QUEUED,
        )
    )

    outcome = await service.dispatch_queue("p1")

    assert outcome.dispatched == () and outcome.failed == ()
    assert outcome.deferred is True
    assert store.queued_control_operation_count("p1") == 1
    (queued_op,) = store.queued_control_operations("p1")
    assert queued_op.delivery_phase is ControlDeliveryPhase.QUEUED
    assert store.get_job("p1#q").state == JobState.RUNNING


async def test_disconnected_native_controls_authorize_before_classification(store: Store):
    """An unauthorized caller learns nothing: authorization refuses first."""
    harness = disconnected_native_harness(store, "p1")
    service = harness.service
    harness.gates_recorder.refuse_authorize_for = {"intruder"}

    async def refuses_at_authorization(coro) -> None:
        try:
            await coro
            raise AssertionError("an unauthorized caller must be refused at authorization")
        except NotYourChild:
            pass

    await refuses_at_authorization(service.send("p1", caller_id="intruder", prompt="s"))
    await refuses_at_authorization(service.queue_followup("p1", caller_id="intruder", prompt="q"))
    await refuses_at_authorization(service.steer("p1", caller_id="intruder", prompt="amend"))
    await refuses_at_authorization(service.update_settings("p1", caller_id="intruder", model="m2"))
    await refuses_at_authorization(service.interrupt("p1", caller_id="intruder"))

    # Authorization refused every control before classification: nothing was
    # revealed, delivered, reserved, queued, or finished.
    _no_operations_or_jobs(store, "p1")
    assert store.queued_control_operations("p1") == []
