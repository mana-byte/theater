"""End-to-end tests for the recording side of recall.

The touch table is populated when the observer feeds ``Event.paths`` into the
``JobManager``'s accumulator, which hashes the files and writes rows at job
end.  These tests drive that path through ``Observer._apply`` — the real hot
path — rather than calling ``observe_paths`` in isolation, because the
unit test of ``observe_paths`` already passes today and the feature is still
dead until ``_apply`` wires it in.
"""

from __future__ import annotations

from unittest.mock import patch

from theater.daemon.blob import blob_sha
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.daemon.schema import touch
from theater.harness.base import Event, EventKind, EventPath
from theater.harness.source import Batch


def _observer_with_jobs(registry, *, cwd: str) -> tuple[Observer, JobManager, str]:
    """An observer wired to a JobManager with one running job.

    The job's target is a registered participant so
    ``oldest_running_job_for_target`` can find it.
    """
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane="%1", cwd=cwd)
    jobs.create(
        handle="job-1",
        caller_id="caller",
        target_id=p.id,
        kind="spawn",
        prompt="do work",
        cwd=cwd,
    )
    observer = Observer(registry, harnesses={}, jobs=jobs)
    return observer, jobs, p.id


def test_events_with_paths_produce_touch_rows(registry, tmp_path):
    """An observed event carrying Event.paths writes touch rows at job end.

    This is the test that proves the feature is no longer inert: the path
    goes through ``_apply`` (the observer's hot path), into the accumulator,
    and comes out as a row in the ``touch`` table with the right job handle,
    path, and mode.
    """
    f = tmp_path / "src.py"
    f.write_bytes(b"original")
    sha_before_expected = blob_sha(f)

    observer, jobs, pid = _observer_with_jobs(registry, cwd=str(tmp_path))

    event = Event(
        kind=EventKind.TOOL_CALL,
        tool_name="edit",
        paths=(EventPath(path="src.py", mode="write"),),
    )
    observer._apply(pid, Batch(events=[event]), QuietClock(), TurnAccumulator())

    f.write_bytes(b"modified")
    jobs.finish("job-1", state="done", result="done")

    rows = list(
        registry.store.conn.execute(
            touch.select().where(touch.c.job_handle == "job-1")
        )
    )
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["job_handle"] == "job-1"
    assert row["path"] == "src.py"
    assert row["mode"] == "write"
    assert row["sha_before"] == sha_before_expected
    # sha_after was computed after the write, so it reflects "modified".
    assert row["sha_after"] == blob_sha(f)


def test_multiple_paths_in_one_batch_all_recorded(registry, tmp_path):
    """Two events with different paths in one batch both land as touch rows."""
    (tmp_path / "a.py").write_bytes(b"a")
    (tmp_path / "b.py").write_bytes(b"b")

    observer, jobs, pid = _observer_with_jobs(registry, cwd=str(tmp_path))

    observer._apply(
        pid,
        Batch(
            events=[
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name="read",
                    paths=(EventPath(path="a.py", mode="read"),),
                ),
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name="write",
                    paths=(EventPath(path="b.py", mode="write"),),
                ),
            ]
        ),
        QuietClock(),
        TurnAccumulator(),
    )
    jobs.finish("job-1", state="done", result="done")

    rows = list(
        registry.store.conn.execute(
            touch.select().where(touch.c.job_handle == "job-1").order_by(touch.c.path)
        )
    )
    assert len(rows) == 2
    assert rows[0]._mapping["path"] == "a.py"
    assert rows[0]._mapping["mode"] == "read"
    assert rows[1]._mapping["path"] == "b.py"
    assert rows[1]._mapping["mode"] == "write"


def test_batch_with_no_paths_does_no_job_lookup(registry):
    """A paths-free batch pays nothing for this feature.

    The guarantee is that ``oldest_running_job_for_target`` is never called
    when no event in the batch carries paths, because that is a database
    query on the hot path of every poll of every watched participant, and
    most batches carry no paths at all.
    """
    observer, _jobs, pid = _observer_with_jobs(registry, cwd="/tmp")

    with patch.object(
        observer.store,
        "oldest_running_job_for_target",
        wraps=observer.store.oldest_running_job_for_target,
    ) as spy:
        observer._apply(
            pid,
            Batch(
                events=[
                    Event(kind=EventKind.ASSISTANT, text="thinking"),
                    Event(kind=EventKind.TOOL_CALL, tool_name="bash"),
                ]
            ),
            QuietClock(),
            TurnAccumulator(),
        )
        assert spy.call_count == 0


def test_batch_with_paths_does_at_most_one_job_lookup(registry, tmp_path):
    """Multiple events with paths in one batch share a single job lookup.

    The handle is resolved once per ``_apply`` call and reused for every
    event in the batch, rather than queried per event.
    """
    (tmp_path / "a.py").write_bytes(b"a")
    (tmp_path / "b.py").write_bytes(b"b")

    observer, _jobs, pid = _observer_with_jobs(registry, cwd=str(tmp_path))

    with patch.object(
        observer.store,
        "oldest_running_job_for_target",
        wraps=observer.store.oldest_running_job_for_target,
    ) as spy:
        observer._apply(
            pid,
            Batch(
                events=[
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name="edit",
                        paths=(EventPath(path="a.py", mode="write"),),
                    ),
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name="edit",
                        paths=(EventPath(path="b.py", mode="write"),),
                    ),
                ]
            ),
            QuietClock(),
            TurnAccumulator(),
        )
        assert spy.call_count == 1


def test_observer_without_jobs_does_not_crash_on_paths(registry, tmp_path):
    """An observer constructed without a JobManager (self.jobs is None) must
    not crash when an event carries paths.  Some construction paths leave
    jobs unset, and the feature must degrade to a no-op there."""
    p = registry.register(harness="vibe", pane="%1", cwd=str(tmp_path))
    observer = Observer(registry, harnesses={})
    assert observer.jobs is None

    event = Event(
        kind=EventKind.TOOL_CALL,
        tool_name="edit",
        paths=(EventPath(path="x.py", mode="write"),),
    )
    observer._apply(p.id, Batch(events=[event]), QuietClock(), TurnAccumulator())
