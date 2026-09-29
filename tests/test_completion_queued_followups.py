"""Completion must leave an undelivered queued followup completely alone.

A pending followup must receive no touches, results, or rescue attention until
it dispatches; the delivered-only selector is the same one dispatch uses.
"""

from __future__ import annotations

from types import SimpleNamespace

from theater.daemon.jobs import JobManager
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observer import QuietClock
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import JobState, now

PROMPT_OBSERVER = SimpleNamespace(
    screen_reading=lambda capture: ScreenReading(
        kind=ScreenKind.PROMPT, confidence=ScreenConfidence.LOW
    )
)


def _op(operation_id, pid, *, kind, phase, job_handle, result=None):
    return ControlOperation(
        operation_id=operation_id,
        participant_id=pid,
        kind=kind,
        transport=ControlTransport.PROVIDER_TERMINAL,
        delivery_phase=phase,
        delivery_result=result,
        job_handle=job_handle,
        created_at=now(),
        updated_at=now(),
    )


def _delivered_plus_queued(registry):
    """Job A delivered (send settled accepted) and followup B still queued."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    jobs.create(handle="a", caller_id="caller", target_id=p.id, kind="send", prompt="a prompt")
    jobs.create(handle="b", caller_id="caller", target_id=p.id, kind="send", prompt="b prompt")
    with registry.store.write_unit() as unit:
        registry.store.reserve_control_operation(
            _op(
                "op-a",
                p.id,
                kind=ControlKind.SEND,
                phase=ControlDeliveryPhase.SETTLED,
                job_handle="a",
                result=DeliveryResult.ACCEPTED,
            ),
            connection=unit.connection,
        )
        registry.store.reserve_control_operation(
            _op(
                "op-b",
                p.id,
                kind=ControlKind.QUEUE_FOLLOWUP,
                phase=ControlDeliveryPhase.QUEUED,
                job_handle="b",
            ),
            connection=unit.connection,
        )
    tracker = CompletionTracker(registry.store, registry, jobs_fn=lambda: jobs)
    return p, jobs, tracker


async def _capture(_pid):
    return "$ "


def _op_phase(registry, operation_id):
    op = registry.store.get_control_operation(operation_id)
    assert op is not None
    return op.delivery_phase


async def test_rescue_finishes_only_the_delivered_job(registry):
    """A quiet bare-prompt rescue must finish A and leave queued B untouched."""
    p, jobs, tracker = _delivered_plus_queued(registry)
    clock = QuietClock()
    clock.last_text = "the last thing it said"

    await tracker.rescue_jobs(
        p.id, PROMPT_OBSERVER, clock, rescue_timeout=60.0, capture_fn=_capture
    )

    a = jobs.get("a")
    assert a.state == JobState.DONE
    assert a.error_code == "turn_end_unseen"
    assert a.result == "the last thing it said"
    b = jobs.get("b")
    assert b.state == JobState.RUNNING
    assert b.result is None
    assert _op_phase(registry, "op-b") == ControlDeliveryPhase.QUEUED


def test_answer_turn_never_touches_a_queued_followup(registry):
    """After A's turn ends, later human turns must not answer or crash B."""
    p, jobs, tracker = _delivered_plus_queued(registry)

    tracker.answer_turn(p.id, "a's answer", heard=["a prompt"])
    a = jobs.get("a")
    assert a.state == JobState.DONE
    assert a.result == "a's answer"

    # Human turns that answer someone else: neither soaked up nor fatal for B.
    tracker.answer_turn(p.id, "human chatter", heard=["unrelated"])
    tracker.answer_turn(p.id, "more chatter", heard=["also unrelated"])
    tracker.answer_turn(p.id, "even more", heard=["still unrelated"])
    b = jobs.get("b")
    assert b.state == JobState.RUNNING
    assert b.result is None
    assert b.error_code is None
    assert _op_phase(registry, "op-b") == ControlDeliveryPhase.QUEUED


async def test_plain_job_without_control_operation_still_completes(registry):
    """A plain running job (no control operation) keeps the old behaviour."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    jobs.create(handle="c", caller_id="caller", target_id=p.id, kind="send", prompt="c prompt")
    tracker = CompletionTracker(registry.store, registry, jobs_fn=lambda: jobs)

    tracker.answer_turn(p.id, "c's answer", heard=["c prompt"])
    assert jobs.get("c").result == "c's answer"

    jobs.create(handle="d", caller_id="caller", target_id=p.id, kind="send", prompt="d prompt")
    clock = QuietClock()
    clock.last_text = "rescued text"
    await tracker.rescue_jobs(
        p.id, PROMPT_OBSERVER, clock, rescue_timeout=60.0, capture_fn=_capture
    )
    d = jobs.get("d")
    assert d.state == JobState.DONE
    assert d.error_code == "turn_end_unseen"
