"""Internal repair of an invalid strict answer: native route, exact turn, durable fence."""

from __future__ import annotations

import json
import threading

import pytest

from tests.rig.fake_runtime import FakeRuntime
from tests.test_control_service import (
    Harness,
    drain,
    make_runtime,
    open_harness,
    state_of,
    wait_until,
    wrap_runtime,
)
from theater.constants.response_contract import REPAIR_DEADLINE_SECONDS, REPAIR_MAX_ATTEMPTS
from theater.daemon.handoff.evidence import EvidencePinner
from theater.daemon.handoff.models import GitFacts
from theater.daemon.persistence.repositories.job_repairs import RepairState
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.persistence.store import Store
from theater.daemon.response_contracts import resolve_response_format
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeCapability,
    RuntimeLifecyclePhase,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import Busy, JobState, Participant, Status, now

VALID = json.dumps({"status": "done", "summary": "Shipped.", "blockers": [], "commit": "abc123"})


async def strict_send(harness: Harness, store: Store, *, queue_after: str | None = None):
    fmt = resolve_response_format(store, {"$theater": "delegate"}, creator_id="caller")
    job = await harness.service.send(
        "p1", caller_id="caller", prompt="ship it", response_format=fmt
    )
    if queue_after is not None:
        await harness.service.queue_followup("p1", caller_id="caller", prompt=queue_after)
    return job


async def end_turn(
    harness: Harness,
    answer: str | None,
    *,
    terminal: NativeTurnTerminal = NativeTurnTerminal.COMPLETED,
    completeness: ResultCompleteness = ResultCompleteness.COMPLETE,
):
    """The active turn finishes with ``answer``: evidence arrives and the backend goes idle."""
    state = state_of(harness, "p1")
    outcome = NativeTurnOutcome(
        native_session_id=state.native_session_id,
        native_turn_id=state.native_turn_id,
        terminal=terminal,
        result=answer,
        completeness=completeness,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )
    state.native_turn_id = None
    return await harness.service.record_terminal_evidence(
        "p1", backend_generation=state.backend_generation, outcome=outcome
    )


async def repair_sent(harness: Harness, count: int) -> None:
    await wait_until(lambda: len(state_of(harness, "p1").sent) >= count)
    await drain()


def audit_events(store: Store, handle: str) -> list[str]:
    return [event for event, _detail in store.contract_audit_for_job(handle)]


def bind(store: Store, harness: Harness, *, generation: int = 1) -> None:
    state = state_of(harness, "p1")
    store.upsert_runtime_binding(
        ParticipantRuntimeBinding(
            participant_id="p1",
            harness="codex",
            wiring=RuntimeWiring.NATIVE,
            backend_generation=generation,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            native_session_id=state.native_session_id,
            created_at=now(),
            updated_at=now(),
        )
    )


# ---- the happy path and its proof ---------------------------------------------------


async def test_invalid_answer_is_repaired_by_the_exact_turn_that_answers_the_repair(store):
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    job = await strict_send(harness, store)

    await end_turn(harness, "all done, see the repo")  # not JSON

    assert store.get_job(job.handle).state == JobState.RUNNING  # one public job, still running
    await repair_sent(harness, 2)
    assert "not accepted" in state.sent[1] and "changed_paths" in state.sent[1]
    attempt = store.repair_attempts_for_job(job.handle)[0]
    assert (attempt.state, attempt.turn_proven) == (RepairState.AWAITING, True)
    assert attempt.native_turn_id == state.native_turn_id != attempt.source_turn_id
    # Safety queries still see the repairing job; the completion matcher does not.
    assert [j.handle for j in store.active_running_jobs_for_target("p1")] == [job.handle]
    assert store.completion_candidate_jobs_for_target("p1") == []

    finished = await end_turn(harness, VALID)

    assert finished.handle == job.handle and finished.state == JobState.DONE
    assert (finished.structured_status, finished.structured_result) == ("parsed", VALID)
    assert not store.repair_fence_active("p1")
    assert audit_events(store, job.handle) == ["repair_started", "repair_succeeded"]


async def test_a_partial_read_of_the_answer_is_repaired_not_trusted(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)

    await end_turn(harness, VALID, completeness=ResultCompleteness.PARTIAL)

    assert store.get_job(job.handle).state == JobState.RUNNING
    await repair_sent(harness, 2)


async def test_repairs_are_bounded_and_the_caller_sees_only_unable_to_parse(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)

    await end_turn(harness, "nope")
    for attempt in range(1, REPAIR_MAX_ATTEMPTS + 1):
        await repair_sent(harness, 1 + attempt)
        finished = await end_turn(harness, f"still wrong {attempt}")

    assert finished.state == JobState.DONE and finished.error_code is None
    assert finished.structured_status == "unable_to_parse" and finished.structured_result is None
    assert finished.result == f"still wrong {REPAIR_MAX_ATTEMPTS}"
    assert len(state_of(harness, "p1").sent) == 1 + REPAIR_MAX_ATTEMPTS
    assert not store.repair_fence_active("p1")
    assert audit_events(store, job.handle)[-1] == "repair_exhausted"


# ---- the fence, priority, and presence --------------------------------------------


async def test_repair_outranks_queued_followups_and_the_fence_refuses_other_prompts(store):
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    job = await strict_send(harness, store, queue_after="later work")

    await end_turn(harness, "garbage")
    await repair_sent(harness, 2)

    assert state.sent[1].startswith("[Theater]")  # the repair, not the queued followup
    assert store.queued_control_operation_count("p1") == 1
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="jump the queue")

    await end_turn(harness, VALID)
    await repair_sent(harness, 3)

    assert state.sent[2] == "later work"  # the followup only runs after the repair resolved
    assert store.get_job(job.handle).structured_status == "parsed"


async def test_presence_still_gates_the_repair_prompt(store):
    harness = await open_harness(store, "p1")
    state = state_of(harness, "p1")
    job = await strict_send(harness, store)
    harness.gates_recorder.presence_refusals.add("p1")

    await end_turn(harness, "garbage")
    await drain()

    assert len(state.sent) == 1  # a human at the terminal: nothing typed into it
    assert store.repair_attempts_for_job(job.handle)[0].state is RepairState.PENDING
    assert store.repair_fence_active("p1")

    harness.gates_recorder.presence_refusals.clear()
    harness.service.schedule_dispatch("p1")
    await repair_sent(harness, 2)


async def test_interrupt_cancels_a_repair_that_was_never_sent(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    harness.gates_recorder.presence_refusals.add("p1")
    await end_turn(harness, "garbage")
    harness.gates_recorder.presence_refusals.clear()

    await harness.service.cancel_queued_followups("p1")

    finished = store.get_job(job.handle)
    assert (finished.state, finished.error_code) == (JobState.KILLED, "interrupted")
    assert not store.repair_fence_active("p1")


# ---- fail closed ------------------------------------------------------------------


async def test_blocked_repair_ends_unable_to_parse_and_hides_the_reason(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    # The capability vanishes after admission, so only the repair is blocked.
    state_of(harness, "p1").unavailable[RuntimeCapability.SEND] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )

    await end_turn(harness, "garbage")
    await wait_until(lambda: store.get_job(job.handle).state == JobState.DONE)

    finished = store.get_job(job.handle)
    assert finished.structured_status == "unable_to_parse" and finished.error_code is None
    assert audit_events(store, job.handle) == ["repair_started", "repair_blocked"]
    assert not store.repair_fence_active("p1")


class _PinSpy(EvidencePinner):
    """Every job leaves evidence; Git is faked, but its thread is recorded."""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.threads: list[int] = []

    def required(self, job) -> bool:
        return True

    def workspace_of(self, job) -> str | None:
        return None

    def pin(self, cwd: str | None) -> GitFacts:
        self.threads.append(threading.get_ident())
        return GitFacts.from_status("c" * 40, "main", 0)


def _pins(harness: Harness, store: Store) -> _PinSpy:
    harness.jobs.evidence = _PinSpy(store)
    return harness.jobs.evidence


@pytest.mark.parametrize("how", ["capability_lost", "receipt_rejected"])
async def test_a_repair_dispatch_that_ends_a_delivered_job_pins_git_off_the_loop(store, how):
    harness = await open_harness(store, "p1")
    pins = _pins(harness, store)
    job = await strict_send(harness, store)
    state = state_of(harness, "p1")
    if how == "capability_lost":
        state.unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.GATED_BY_BACKEND
    else:
        state.reject_code = "refused"

    await end_turn(harness, "garbage")
    await wait_until(lambda: store.get_job(job.handle).state == JobState.DONE)

    assert store.get_job(job.handle).structured_status == "unable_to_parse"
    evidence = store.get_job_evidence(job.handle)
    assert evidence.git.reviewable, evidence.git.reason
    assert pins.threads and threading.get_ident() not in pins.threads


async def test_a_deferred_repair_poll_costs_no_git(store):
    harness = await open_harness(store, "p1")
    pins = _pins(harness, store)
    job = await strict_send(harness, store)
    harness.gates_recorder.presence_refusals.add("p1")  # the repair waits for the human to leave
    await end_turn(harness, "garbage")
    pins.threads.clear()  # the strict job's own non-terminal evidence reads are not under test

    await harness.service.maintain_repairs("p1")

    assert store.get_job(job.handle).state == JobState.RUNNING and pins.threads == []


class LostAckRuntime(FakeRuntime):
    """Transmits a prompt but loses its acknowledgement once armed."""

    armed = False

    async def send(self, *, operation_id: str, prompt: str):
        receipt = await super().send(operation_id=operation_id, prompt=prompt)
        if self.armed:
            raise ConnectionError("acknowledgement lost")
        return receipt


async def test_unknown_repair_delivery_ends_the_job_but_only_positive_evidence_lifts_the_fence(
    store,
):
    store.upsert_participant(Participant(id="p1", harness="codex"))
    harness = Harness(store, {"p1": wrap_runtime(make_runtime("p1"), LostAckRuntime)})
    await harness.runtimes["p1"].open_session(mode=SessionOpenMode.NEW)
    bind(store, harness)
    job = await strict_send(harness, store)
    harness.runtimes["p1"].armed = True

    await end_turn(harness, "garbage")
    await repair_sent(harness, 2)
    attempt = store.repair_attempts_for_job(job.handle)[0]
    assert attempt.state is RepairState.UNKNOWN  # never resent, never provable

    harness.service._clock = lambda: now() + 3600  # past the unknown-delivery deadline
    state_of(harness, "p1").native_turn_id = None  # and the backend reads idle
    await harness.service.maintain_repairs("p1")

    finished = store.get_job(job.handle)
    assert finished.structured_status == "unable_to_parse"
    assert audit_events(store, job.handle)[-1] == "repair_unknown"
    assert len(state_of(harness, "p1").sent) == 2  # no replay
    assert store.repair_fence_active("p1")  # idle alone proves nothing
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="next")

    bind(store, harness, generation=2)  # a new backend generation: the old turn is gone
    await harness.service.maintain_repairs("p1")
    assert not store.repair_fence_active("p1")


async def test_restart_never_replays_a_dispatched_repair_and_resolves_it_from_evidence(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    await end_turn(harness, "garbage")
    await repair_sent(harness, 2)
    attempt = store.repair_attempts_for_job(job.handle)[0]
    store.update_repair_attempt(
        attempt.attempt_id, updated_at=now(), state=RepairState.DISPATCHED, native_turn_id=None
    )

    harness.service.settle_repairs_at_restart(["p1"])

    assert store.repair_attempts_for_job(job.handle)[0].state is RepairState.UNKNOWN
    assert len(state_of(harness, "p1").sent) == 2


async def test_dead_participant_releases_its_fence(store):
    store.upsert_participant(Participant(id="p1", harness="codex"))
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    await end_turn(harness, "garbage")
    await repair_sent(harness, 2)
    assert store.repair_fence_active("p1")

    store.set_status("p1", Status.DEAD)
    await harness.service.maintain_repairs("p1")

    assert not store.repair_fence_active("p1")
    assert store.get_job(job.handle).structured_status == "unable_to_parse"


async def test_a_pending_repair_survives_a_restart_and_maintenance_sends_it(theater_home):
    path = theater_home / "repair-restart.sqlite"
    first = Store(path)
    try:
        harness = await open_harness(first, "p1")
        job = await strict_send(harness, first)
        harness.gates_recorder.presence_refusals.add("p1")  # a human holds the terminal
        await end_turn(harness, "garbage")
        await drain()
        session = state_of(harness, "p1").native_session_id
        assert first.repair_attempts_for_job(job.handle)[0].state is RepairState.PENDING
    finally:
        first.close()

    second = Store(path)
    try:
        runtime = make_runtime("p1")
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id=session)
        restarted = Harness(second, {"p1": runtime})
        restarted.service.start(["p1"])

        await repair_sent(restarted, 1)

        assert second.repair_attempts_for_job(job.handle)[0].state is RepairState.AWAITING
        assert second.get_job(job.handle).state == JobState.RUNNING
        await restarted.service.aclose()
    finally:
        second.close()


# ---- the whole-repair deadline ----------------------------------------------------


async def test_a_repair_that_never_reached_the_wire_ends_at_the_deadline_and_lifts_its_fence(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    harness.gates_recorder.presence_refusals.add("p1")  # a human holds the terminal throughout
    await end_turn(harness, "garbage")
    await drain()
    assert store.repair_fence_active("p1")

    harness.service._clock = lambda: now() + REPAIR_DEADLINE_SECONDS + 1
    await harness.service.maintain_repairs("p1")

    assert store.get_job(job.handle).structured_status == "unable_to_parse"
    assert audit_events(store, job.handle)[-1] == "repair_timeout"
    assert len(state_of(harness, "p1").sent) == 1  # nothing was typed into the protected terminal
    assert not store.repair_fence_active("p1")  # nothing was sent, so nothing can still run


async def test_a_repair_turn_past_the_deadline_ends_the_job_but_keeps_the_fence_for_its_turn(store):
    harness = await open_harness(store, "p1")
    job = await strict_send(harness, store)
    await end_turn(harness, "garbage")
    await repair_sent(harness, 2)  # the repair turn is running and proven

    harness.service._clock = lambda: now() + REPAIR_DEADLINE_SECONDS + 1
    await harness.service.maintain_repairs("p1")

    finished = store.get_job(job.handle)
    assert finished.state == JobState.DONE and finished.structured_status == "unable_to_parse"
    assert audit_events(store, job.handle)[-1] == "repair_timeout"
    assert store.repair_fence_active("p1")  # the turn may still be typing; idle proves nothing
    with pytest.raises(Busy):
        await harness.service.send("p1", caller_id="caller", prompt="next")

    await end_turn(harness, VALID)  # its evidence finally arrives: the fence lifts, the job stays

    assert not store.repair_fence_active("p1")
    assert store.get_job(job.handle).structured_status == "unable_to_parse"


async def test_a_stored_schema_the_validator_cannot_run_fails_without_repair(store):
    """A snapshot from before the keyword rules: evidence must not raise and strand the job."""
    harness = await open_harness(store, "p1")
    legacy = json.dumps(
        {"$theater": "custom", "name": "old", "schema": {"$ref": "#/required", "required": ["a"]}}
    )
    job = await harness.service.send("p1", caller_id="caller", prompt="go", response_format=legacy)

    await end_turn(harness, '{"a": 1}')

    finished = store.get_job(job.handle)
    assert finished.state == JobState.DONE
    assert finished.structured_status == "unable_to_parse"
    assert store.repair_attempts_for_job(job.handle) == []
    assert audit_events(store, job.handle)[-1] == "contract_unavailable"
