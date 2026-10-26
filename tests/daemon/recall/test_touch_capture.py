"""The touch table and accumulator, and how observed event paths reach it."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import theater.daemon.jobs as jobs_mod
from tests.support.observation import apply_batch
from theater.constants.daemon import TOUCH_HASH_MAX_FILE_BYTES
from theater.daemon.blob import BlobHash, BlobHashState, blob_sha
from theater.daemon.jobs import JobManager, TouchAccumulator
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.daemon.recall import recall
from theater.daemon.schema import touch
from theater.daemon.touch_paths import normalize_touch_path
from theater.harness.base import Event, EventKind, EventPath
from theater.harness.source import Batch
from theater.models import JobState


def test_touch_path_validation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert normalize_touch_path(root, str(tmp_path / "outside")) is None
    assert normalize_touch_path(root, "../escape") is None
    assert normalize_touch_path(root, "foo/../bar.py") == "bar.py"
    acc = TouchAccumulator(cwd=str(root))
    acc.observe((EventPath(path="foo/../bar.py", mode="read"),))
    assert acc.rows("h1")[0]["path"] == "bar.py"

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    assert normalize_touch_path(root, "escape/file.py") is None

    (root / "other" / "sub").mkdir(parents=True)
    (root / "link").symlink_to(root / "other" / "sub", target_is_directory=True)
    assert normalize_touch_path(root, "link/../file.py") is None


def test_touch_accumulator_skips_rejected_paths(tmp_path):
    acc = TouchAccumulator(cwd=str(tmp_path))
    acc.observe((EventPath(path="../escape", mode="read"),))
    assert acc.rows("h1") == []


def test_touch_accumulator_revalidates_before_hashing(tmp_path):
    inside = tmp_path / "inside"
    outside = tmp_path.parent
    inside.mkdir()
    (inside / "file.py").write_bytes(b"inside")
    link = tmp_path / "link"
    link.symlink_to(inside, target_is_directory=True)

    acc = TouchAccumulator(cwd=str(tmp_path))
    acc.observe((EventPath(path="link/file.py", mode="read"),))
    link.unlink()
    link.symlink_to(outside, target_is_directory=True)

    row = acc.rows("h1")[0]
    assert row["sha_before"] is not None
    assert row["sha_after"] is None
    assert row["sha_after_error"] == "unsafe_path"


def test_touch_accumulator_captures_sha_before_on_first_sight(tmp_path):
    """sha_before is the hash at first sight, not at last sight.

    A path seen twice does not re-hash for sha_before: the before state is
    the state when the job first saw the file, and re-hashing would
    overwrite it with a mid-job hash that says nothing about what the job
    started from.
    """
    f = tmp_path / "file.txt"
    f.write_bytes(b"original")

    acc = TouchAccumulator(cwd=str(tmp_path))
    acc.observe((EventPath(path="file.txt", mode="read"),))

    sha_before_expected = blob_sha(f)

    # Modify the file after first sight.
    f.write_bytes(b"modified")

    # Observe again — sha_before must not change.
    acc.observe((EventPath(path="file.txt", mode="write"),))

    rows = acc.rows("h1")
    assert len(rows) == 1
    assert rows[0]["sha_before"] == sha_before_expected
    assert rows[0]["sha_after"] == blob_sha(f)
    assert rows[0]["mode"] == "write"  # last write wins


def test_touch_accumulator_sha_after_none_for_deleted_file(tmp_path):
    """A file deleted during the job has sha_after = None."""
    f = tmp_path / "doomed.txt"
    f.write_bytes(b"doomed")

    acc = TouchAccumulator(cwd=str(tmp_path))
    acc.observe((EventPath(path="doomed.txt", mode="write"),))

    f.unlink()

    rows = acc.rows("h1")
    assert len(rows) == 1
    assert rows[0]["sha_before"] is not None
    assert rows[0]["sha_after"] is None


def test_touch_accumulator_sha_before_none_for_created_file(tmp_path):
    """A file created during the job has sha_before = None."""
    acc = TouchAccumulator(cwd=str(tmp_path))
    # File does not exist yet.
    acc.observe((EventPath(path="born.txt", mode="write"),))

    f = tmp_path / "born.txt"
    f.write_bytes(b"born")

    rows = acc.rows("h1")
    assert len(rows) == 1
    assert rows[0]["sha_before"] is None
    assert rows[0]["sha_after"] == blob_sha(f)


def test_touch_accumulator_empty_when_no_paths_observed(tmp_path):
    """An accumulator that saw nothing is falsy and yields no rows."""
    acc = TouchAccumulator(cwd=str(tmp_path))
    assert not acc
    assert acc.rows("h1") == []


def test_touch_accumulator_records_unavailable_hash_instead_of_claiming_deletion(tmp_path):
    path = tmp_path / "large.bin"
    path.touch()
    with path.open("r+b") as stream:
        stream.truncate(TOUCH_HASH_MAX_FILE_BYTES + 1)
    acc = TouchAccumulator(cwd=str(tmp_path))

    acc.observe((EventPath(path="large.bin", mode="write"),))

    row = acc.rows("h1")[0]
    assert row["sha_before"] is None
    assert row["sha_after"] is None
    assert row["sha_before_error"] == "too_large"
    assert row["sha_after_error"] == "too_large"


def test_touch_accumulator_bounds_total_hash_work(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "TOUCH_HASH_MAX_FILE_BYTES", 2)
    monkeypatch.setattr(jobs_mod, "TOUCH_HASH_MAX_JOB_BYTES", 3)
    budgets: list[int] = []

    def bounded_hash(path, *, max_bytes):
        budgets.append(max_bytes)
        if max_bytes == 0:
            return BlobHash(BlobHashState.UNAVAILABLE, reason="too_large")
        return BlobHash(BlobHashState.HASHED, digest=path.name, size=max_bytes)

    monkeypatch.setattr(jobs_mod, "blob_hash", bounded_hash)
    acc = TouchAccumulator(cwd=str(tmp_path))
    acc.observe(
        tuple(EventPath(path=f"{name}.txt", mode="write") for name in ("one", "two", "three"))
    )

    rows = acc.rows("h1")

    assert budgets == [2, 1, 0, 2, 1, 0]
    assert [row["path"] for row in rows] == ["one.txt", "two.txt", "three.txt"]
    assert rows[2]["sha_before_error"] == "too_large"
    assert rows[2]["sha_after_error"] == "too_large"


def test_record_touches_writes_in_same_transaction_as_job_result(store, tmp_path):
    """finish() writes the job result and touch rows atomically.

    A job whose result committed but whose touches did not is a silent gap
    in the timeline. The transactional write prevents that.
    """
    f = tmp_path / "touched.py"
    f.write_bytes(b"original")

    jobs = JobManager(store)
    job = jobs.create(
        handle="h1",
        caller_id="caller",
        target_id="target",
        kind="spawn",
        prompt="do work",
        cwd=str(tmp_path),
    )
    assert job.state == JobState.RUNNING

    # Feed paths into the accumulator, then modify the file so sha_after
    # differs from sha_before.
    sha_before_expected = blob_sha(f)
    jobs.observe_paths("h1", (EventPath(path="touched.py", mode="write"),))
    f.write_bytes(b"modified")

    jobs.finish("h1", state=JobState.DONE, result="done")

    # The job is done.
    finished = jobs.get("h1")
    assert finished is not None
    assert finished.state == JobState.DONE

    # The touch row was written in the same transaction.
    rows = list(store.conn.execute(touch.select().where(touch.c.job_handle == "h1")))
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["path"] == "touched.py"
    assert row["mode"] == "write"
    assert row["sha_before"] == sha_before_expected
    assert row["sha_after"] == blob_sha(f)


def test_finish_without_touches_takes_plain_path(store, tmp_path):
    """A job with no observed paths finishes without opening a transaction."""
    jobs = JobManager(store)
    jobs.create(
        handle="h2",
        caller_id="caller",
        target_id="target",
        kind="spawn",
        prompt="no files",
        cwd=str(tmp_path),
    )
    jobs.finish("h2", state=JobState.DONE, result="done")

    assert jobs.get("h2").state == JobState.DONE
    rows = list(store.conn.execute(touch.select()))
    assert len(rows) == 0


def test_finish_without_cwd_has_no_accumulator(store):
    """A job created without cwd gets no accumulator — nothing to resolve paths against."""
    jobs = JobManager(store)
    jobs.create(
        handle="h3",
        caller_id="caller",
        target_id="target",
        kind="spawn",
    )
    # observe_paths on a job with no accumulator is a no-op.
    jobs.observe_paths("h3", (EventPath(path="foo.py", mode="read"),))
    jobs.finish("h3", state=JobState.DONE, result="done")

    assert jobs.get("h3").state == JobState.DONE


def test_incomplete_staging_fails_loudly_before_mutating(tmp_path):
    from theater.daemon.jobs import StagedHashes, StagingIncomplete

    acc = TouchAccumulator(cwd=str(tmp_path))

    with pytest.raises(StagingIncomplete):
        acc.observe((EventPath(path="x.txt", mode="write"),), staged=StagedHashes())

    assert acc.known_paths() == []


def test_staging_bounds_synchronous_hash_work_for_one_job(store, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "TOUCH_HASH_MAX_JOB_BYTES", 10)
    hashed: list[int] = []
    real = jobs_mod.blob_hash

    def counting(path, *, max_bytes):
        outcome = real(path, max_bytes=max_bytes)
        if outcome.state is BlobHashState.HASHED:
            hashed.append(outcome.size)
        return outcome

    monkeypatch.setattr(jobs_mod, "blob_hash", counting)
    names = [f"f{i}.txt" for i in range(50)]
    for name in names:
        (tmp_path / name).write_bytes(b"1234")
    jobs = JobManager(store)
    jobs.create(handle="h1", caller_id="cli", target_id="t", kind="send", cwd=str(tmp_path))

    with jobs.stage_hashes("t", tuple(EventPath(path=n, mode="write") for n in names)):
        pass

    assert 0 < sum(hashed) <= 2 * 10


def _stage(jobs, target, cwd_paths):
    with jobs.stage_hashes(target, cwd_paths):
        return jobs._staged


def test_staging_known_paths_spend_only_the_after_side_budget(store, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "TOUCH_HASH_MAX_JOB_BYTES", 10)
    jobs = JobManager(store)
    jobs.create(handle="h1", caller_id="cli", target_id="t", kind="send", cwd=str(tmp_path))
    for name in ("k1.txt", "k2.txt"):
        (tmp_path / name).write_bytes(b"1")
        jobs.observe_paths("h1", (EventPath(path=name, mode="write"),))
        (tmp_path / name).write_bytes(b"123456789")
    (tmp_path / "new.txt").write_bytes(b"1234")

    staged = _stage(jobs, "t", (EventPath(path="new.txt", mode="write"),))

    assert staged.hashes[(str(tmp_path), "k1.txt")].state is BlobHashState.HASHED
    assert staged.hashes[(str(tmp_path), "k2.txt")].reason == "too_large"
    assert staged.hashes[(str(tmp_path), "new.txt")].state is BlobHashState.HASHED


def test_staging_budgets_are_never_shared_between_jobs(store, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "TOUCH_HASH_MAX_JOB_BYTES", 10)
    cwd_a, cwd_b = tmp_path / "a", tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    jobs = JobManager(store)
    jobs.create(handle="hb", caller_id="cli", target_id="t", kind="send", cwd=str(cwd_b))
    jobs.create(handle="ha", caller_id="cli", target_id="t", kind="send", cwd=str(cwd_a))
    heavy = [f"heavy{i}.txt" for i in range(6)]
    for name in heavy:
        (cwd_a / name).write_bytes(b"12345678")
    (cwd_b / "small.txt").write_bytes(b"1234")
    paths = tuple(EventPath(path=n, mode="write") for n in [*heavy, "small.txt"])

    staged = _stage(jobs, "t", paths)

    assert staged.hashes[(str(cwd_b), "small.txt")].state is BlobHashState.HASHED


def _observer_with_jobs(registry, *, cwd: str) -> tuple[Observer, JobManager, str]:
    """An observer wired to a JobManager with one running job.

    The job's target is a registered participant so
    ``oldest_running_job_for_target`` can find it.
    """
    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd=cwd)
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
    apply_batch(observer, pid, Batch(events=[event]), QuietClock(), TurnAccumulator())

    f.write_bytes(b"modified")
    jobs.finish("job-1", state="done", result="done")

    rows = list(registry.store.conn.execute(touch.select().where(touch.c.job_handle == "job-1")))
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["job_handle"] == "job-1"
    assert row["path"] == "src.py"
    assert row["mode"] == "write"
    assert row["sha_before"] == sha_before_expected
    # sha_after was computed after the write, so it reflects "modified".
    assert row["sha_after"] == blob_sha(f)


def test_unavailable_hash_remains_recallable(registry, tmp_path):
    path = tmp_path / "large.lock"
    path.touch()
    with path.open("r+b") as stream:
        stream.truncate(TOUCH_HASH_MAX_FILE_BYTES + 1)
    observer, jobs, pid = _observer_with_jobs(registry, cwd=str(tmp_path))
    apply_batch(
        observer,
        pid,
        Batch(
            events=[
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name="write",
                    paths=(EventPath(path="large.lock", mode="write"),),
                )
            ]
        ),
        QuietClock(),
        TurnAccumulator(),
    )
    jobs.finish("job-1", state="done", result="done")

    point = recall(registry.store, paths=["large.lock"], caller_cwd=str(tmp_path))["large.lock"][
        "timeline"
    ][0]
    assert point["handle"] == "job-1"
    assert point["sha_before_error"] == "too_large"
    assert point["sha_after_error"] == "too_large"


def test_multiple_paths_in_one_batch_all_recorded(registry, tmp_path):
    """Two events with different paths in one batch both land as touch rows."""
    (tmp_path / "a.py").write_bytes(b"a")
    (tmp_path / "b.py").write_bytes(b"b")

    observer, jobs, pid = _observer_with_jobs(registry, cwd=str(tmp_path))

    apply_batch(
        observer,
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
        apply_batch(
            observer,
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
        apply_batch(
            observer,
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
    p = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    observer = Observer(registry, harnesses={})
    assert observer.jobs is None

    event = Event(
        kind=EventKind.TOOL_CALL,
        tool_name="edit",
        paths=(EventPath(path="x.py", mode="write"),),
    )
    apply_batch(observer, p.id, Batch(events=[event]), QuietClock(), TurnAccumulator())
