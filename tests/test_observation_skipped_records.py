"""A skipped transcript record is reported without failing the channel or its jobs."""

from types import SimpleNamespace

from theater.daemon.observation.failures import FailureTracker
from theater.harness.contracts.source import Batch


def test_skipped_record_is_reported_but_never_crashes_a_running_job():
    bus: list[tuple[str, dict]] = []
    job = SimpleNamespace(handle="job-a", created_at=0.0)
    store = SimpleNamespace(
        bus_append=lambda kind, *, to_id, payload: bus.append((kind, payload)),
        running_jobs_for_target=lambda _pid: [job],
    )
    tracker = FailureTracker(
        store, None, wall_now_fn=lambda: 1_000.0, grace_fn=lambda: 1.0, jobs_fn=object
    )
    finished: list[str] = []

    tracker.handle_source_error(
        "participant-a",
        Batch(error_code="pi_transcript_oversized_record", error="record too large"),
        finish_fn=lambda handle, *_args, **_kwargs: finished.append(handle),
    )

    assert finished == []
    assert not tracker.has_source_error("participant-a", "pi_transcript_oversized_record")
    assert bus == [
        (
            "agent.observation_error",
            {"code": "pi_transcript_oversized_record", "message": "record too large"},
        )
    ]
