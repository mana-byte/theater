"""The resume floor stops stale attach-derived status and completion from landing."""

from __future__ import annotations

from tests.support.observation import apply_batch, poised
from tests.support.observer_rigs import said
from theater.daemon.observer import (
    Observer,
    TurnAccumulator,
)
from theater.harness.source import Attachment, Batch, StreamPoint
from theater.models import (
    Status,
)
from theater.resume_floor import (
    UNKNOWN_FLOOR,
    encode_floor,
    floor_is_present,
)


def test_resume_floor_suppresses_attach_status_and_completion(registry):
    """A present-but-unknown floor suppresses last_event-derived status/completion.

    Deterministic: calls _on_attach directly with a Batch-constructed Attachment,
    not through the async watcher. The floor must NOT be cleared by a suppressed
    attach — it survives so reattachment after restart still suppresses.
    """
    observer, _screen, _clock, p, jobs = poised(registry)
    floor_raw = UNKNOWN_FLOOR
    p.resume_floor = floor_raw
    registry.store.upsert_participant(p)
    event = said("predecessor reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=5,
        last_event=event,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    # Status did not move from the default — the turn_end was suppressed.
    assert registry.get(p.id).status is Status.IDLE
    # No agent.assistant or agent.user events from the attach — only agent.transcript.
    agent_kinds = [
        r["kind"]
        for r in registry.store.bus_tail(limit=500)
        if r["to_id"] == p.id and r["kind"].startswith("agent.")
    ]
    assert agent_kinds == ["agent.transcript"]
    # The job was NOT completed by the predecessor's turn_end.
    assert jobs.get("h1").state == "running"
    # The floor was NOT cleared — suppressed attach keeps it.
    reloaded = registry.store.get_participant(p.id)
    assert reloaded.resume_floor == floor_raw


def test_attachment_status_settles_participant_without_completing_job(registry):
    observer, _screen, _clock, p, jobs = poised(registry)
    registry.set_status(p.id, Status.WORKING)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        status=Status.IDLE,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )

    observer._on_attach(p.id, attached)

    assert registry.get(p.id).status is Status.IDLE
    assert jobs.get("h1").state == "running"


def test_resume_floor_suppresses_stale_attachment_status(registry):
    observer, _screen, _clock, p, jobs = poised(registry)
    p.status = Status.WORKING
    p.resume_floor = UNKNOWN_FLOOR
    registry.store.upsert_participant(p)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        status=Status.IDLE,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )

    observer._on_attach(p.id, attached)

    reloaded = registry.store.get_participant(p.id)
    assert reloaded.status is Status.WORKING
    assert reloaded.resume_floor == UNKNOWN_FLOOR
    assert jobs.get("h1").state == "running"


def test_post_floor_attachment_status_settles_and_clears_floor(registry):
    observer, _screen, _clock, p, jobs = poised(registry)
    p.status = Status.WORKING
    p.resume_floor = encode_floor(StreamPoint(records=3, size=60, dev=10, ino=20))
    registry.store.upsert_participant(p)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        status=Status.IDLE,
        point=StreamPoint(records=4, size=80, dev=10, ino=20),
    )

    observer._on_attach(p.id, attached)

    reloaded = registry.store.get_participant(p.id)
    assert reloaded.status is Status.IDLE
    assert reloaded.resume_floor is None
    assert jobs.get("h1").state == "running"


def test_structured_floor_suppresses_at_equal_records(registry):
    """A structured floor at equal records suppresses — not strictly beyond.

    The predecessor's transcript ended at N records with a turn_end. The
    successor attaches at the same N records (no new content yet). The floor
    is NOT authorised because point.records is not > floor.records.
    """
    observer, _screen, _clock, p, jobs = poised(registry)
    floor = StreamPoint(records=5, size=100, dev=10, ino=20)
    p.resume_floor = encode_floor(floor)
    registry.store.upsert_participant(p)
    event = said("predecessor reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=5,
        last_event=event,
        # Same stream, same size, same records -> NOT strictly beyond.
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    assert jobs.get("h1").state == "running"
    reloaded = registry.store.get_participant(p.id)
    assert floor_is_present(reloaded.resume_floor)


def test_null_floor_preserves_cold_fast_spawn(registry):
    """A NULL floor (cold spawn) preserves the existing attach behaviour."""
    observer, _screen, _clock, p, jobs = poised(registry)
    event = said("done", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=1,
        last_event=event,
        point=StreamPoint(records=1, size=50, dev=1, ino=2),
    )
    observer._on_attach(p.id, attached)
    # Cold spawn: status settled, job completed.
    assert registry.get(p.id).status is Status.IDLE
    assert jobs.get("h1").state == "done"
    reloaded = registry.store.get_participant(p.id)
    assert reloaded.resume_floor is None


def test_authorized_attach_clears_floor_without_reverting_status(registry):
    """An authorized attach clears the floor via targeted update.

    _settle moves status/last_activity; the floor clear must not revert them
    by replaying a stale Participant snapshot.
    """
    observer, _screen, _clock, p, jobs = poised(registry)
    floor = StreamPoint(records=3, size=60, dev=10, ino=20)
    p.resume_floor = encode_floor(floor)
    registry.store.upsert_participant(p)
    # Set a known status before attach so we can check it is not reverted.
    registry.set_status(p.id, Status.WORKING)
    pre_activity = registry.store.get_participant(p.id).last_activity
    event = said("new reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=10,
        last_event=event,
        # Same stream, larger size, more records -> authorised.
        point=StreamPoint(records=10, size=200, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    # Status moved to IDLE (turn_end) — not reverted to WORKING.
    assert registry.get(p.id).status is Status.IDLE
    # The job was completed.
    assert jobs.get("h1").state == "done"
    # The floor was cleared via targeted update.
    reloaded = registry.store.get_participant(p.id)
    assert reloaded.resume_floor is None
    # last_activity was NOT reverted to the pre-attach snapshot.
    assert reloaded.last_activity >= pre_activity


def test_suppressed_floor_survives_observer_restart(registry):
    """A suppressed floor survives observer close/reopen and still suppresses.

    The observer is closed and a new one is constructed against the same store.
    The floor is still present and still suppresses.
    """
    observer, _screen, _clock, p, jobs = poised(registry)
    p.resume_floor = UNKNOWN_FLOOR
    registry.store.upsert_participant(p)
    event = said("predecessor reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=5,
        last_event=event,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    assert jobs.get("h1").state == "running"
    # Close and reopen the observer.
    # (Observer has no aclose needed for synchronous tests — just make a new one.)
    observer2 = Observer(registry, harnesses={}, jobs=jobs)
    observer2._capture = observer._capture
    # The floor is still present.
    reloaded = registry.store.get_participant(p.id)
    assert floor_is_present(reloaded.resume_floor)
    # Re-attach with the same predecessor event — still suppressed.
    observer2._on_attach(p.id, attached)
    assert jobs.get("h1").state == "running"
    # Floor still present — suppressed attach does not clear.
    reloaded = registry.store.get_participant(p.id)
    assert floor_is_present(reloaded.resume_floor)


def test_post_floor_drained_progress_clears_floor(registry):
    """After a suppressed attach, later drained progress clears the floor.

    A non-attachment batch with events/progressed from the committed cursor
    is necessarily post-attach/post-launch. The floor is cleared via targeted
    update, and the new turn completes the job.
    """
    observer, _screen, clock, p, jobs = poised(registry)
    p.resume_floor = UNKNOWN_FLOOR
    registry.store.upsert_participant(p)
    # First: suppressed attach.
    event = said("predecessor reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=5,
        last_event=event,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    assert jobs.get("h1").state == "running"
    assert floor_is_present(registry.store.get_participant(p.id).resume_floor)
    # Now: a non-attachment batch with actual progress (new events).
    new_event = said("successor reply", turn_end=True)
    batch = Batch(events=[new_event], progressed=True)
    apply_batch(observer, p.id, batch, clock, TurnAccumulator())
    # The floor was cleared.
    reloaded = registry.store.get_participant(p.id)
    assert reloaded.resume_floor is None
    # The new turn completed the job with the successor's text.
    assert jobs.get("h1").state == "done"
    assert jobs.get("h1").result == "successor reply"


def test_empty_poll_does_not_clear_floor(registry):
    """An empty or status-only poll does not clear the floor."""
    observer, _screen, clock, p, _jobs = poised(registry)
    p.resume_floor = UNKNOWN_FLOOR
    registry.store.upsert_participant(p)
    # Suppressed attach.
    event = said("predecessor reply", turn_end=True)
    attached = Attachment(
        location="/tmp/transcript.jsonl",
        session_id=None,
        skipped=5,
        last_event=event,
        point=StreamPoint(records=5, size=100, dev=10, ino=20),
    )
    observer._on_attach(p.id, attached)
    # An empty batch — no progress, no events.
    apply_batch(observer, p.id, Batch(), clock, TurnAccumulator())
    # Floor is still present.
    reloaded = registry.store.get_participant(p.id)
    assert floor_is_present(reloaded.resume_floor)
