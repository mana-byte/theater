"""Tests for the touch table and the per-job path accumulator.

The touch table records which files each job touched, with content hashes
before and after so recall can detect drift. These tests cover the migration
round-trip, the accumulator's sha_before/sha_after semantics, and the
transactional write that binds touches to the job result.
"""

from __future__ import annotations

from theater.daemon.blob import blob_sha
from theater.daemon.jobs import JobManager, TouchAccumulator
from theater.daemon.schema import touch
from theater.harness.base import EventPath
from theater.models import JobState


def test_touch_indexes_exist(store):
    """The two read patterns have indexes: by path and by job handle."""
    indexes = set(
        store.conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='touch'"
        ).scalars()
    )
    assert {"idx_touch_path", "idx_touch_job"} <= indexes


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


def test_busy_timeout_is_set_on_all_connections(store):
    """The pragma is set on both the long-lived connection and fresh ones.

    `_finish_with_touches` opens a fresh connection from the store's engine,
    so busy_timeout must be applied by the connect event listener — not just
    on the long-lived connection. Testing the pragma value directly rather
    than engineering a contended-write race: a race test would need precise
    timing to be honest, and a test that always passes teaches nothing.
    """
    assert store.conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    with store.engine.connect() as fresh:
        assert fresh.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
