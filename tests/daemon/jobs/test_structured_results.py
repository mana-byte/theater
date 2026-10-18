"""Structured JSON results: raw and parsed forms, unavailable cases and commit atomicity."""

from __future__ import annotations

import json

import pytest


def test_structured_json_keeps_full_raw_result_and_clipped_legacy_result(store):
    from theater.daemon.jobs import JobManager, JobState
    from theater.harness.base import MAX_TEXT, clip

    raw = json.dumps({"answer": "x" * (MAX_TEXT + 500)})
    assert len(raw) > MAX_TEXT

    jm = JobManager(store)
    jm.create(
        handle="json-long",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format='{"type":"json_schema"}',
    )
    jm.finish(
        "json-long",
        state=JobState.DONE,
        result=clip(raw),
        raw_result=raw,
    )

    job = jm.get("json-long")
    assert job is not None
    assert job.result == clip(raw)
    assert job.structured_result == raw
    assert json.loads(job.structured_result) == {"answer": "x" * (MAX_TEXT + 500)}
    assert job.structured_status == "parsed"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("{not json}", id="invalid-json"),
        pytest.param('```json\n{"answer": 42}\n```', id="fenced"),
        pytest.param('{"answer": 42}\ntrailing prose', id="trailing-prose"),
        pytest.param("[" * 2000 + "0" + "]" * 2000, id="recursion-error"),
        pytest.param("NaN", id="nan"),
        pytest.param("1e10000", id="infinity"),
        pytest.param("[1e10000]", id="nested-infinity"),
    ],
)
def test_structured_json_that_does_not_parse_is_unavailable(store, raw):
    from theater.daemon.jobs import JobManager, JobState

    jm = JobManager(store)
    jm.create(
        handle="bad-json",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    finished = jm.finish("bad-json", state=JobState.DONE, result=raw[:20], raw_result=raw)

    for job in (finished, jm.get("bad-json")):
        assert job is not None
        assert job.structured_result is None
        assert job.structured_status == "unavailable"


@pytest.mark.parametrize(
    ("response_format", "expected"),
    [
        ("json", "unavailable"),
        (json.dumps({"$theater": "custom", "schema": {"type": "number"}}), "unable_to_parse"),
    ],
)
def test_jobs_rpc_corrects_corrupt_parsed_rows_without_changing_storage(response_format, expected):
    from theater.daemon.rpc.jobs import _job_to_dict
    from theater.models import Job

    job = Job(
        handle="legacy",
        caller_id="cli",
        target_id="target",
        kind="send",
        prompt="answer",
        state="done",
        result="1e10000",
        error_code=None,
        created_at=1.0,
        finished_at=2.0,
        response_format=response_format,
        structured_result="1e10000",
        structured_status="parsed",
    )
    row = _job_to_dict(job)
    assert (row["structured_status"], row["structured_result"]) == (expected, None)
    assert (job.structured_status, job.structured_result) == ("parsed", "1e10000")


def test_structured_json_null_is_a_parsed_raw_result(store):
    from theater.daemon.jobs import JobManager, JobState

    jm = JobManager(store)
    jm.create(
        handle="json-null",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    jm.finish("json-null", state=JobState.DONE, result="null", raw_result="null")

    job = jm.get("json-null")
    assert job is not None
    assert job.structured_result == "null"
    assert json.loads(job.structured_result) is None
    assert job.structured_status == "parsed"


@pytest.mark.parametrize(
    "state,error_code",
    [
        ("killed", "killed"),
        ("crashed", "crashed"),
        ("crashed", "send_failed"),
    ],
)
def test_structured_json_terminal_failures_are_unavailable(store, state, error_code):
    from theater.daemon.jobs import JobManager

    jm = JobManager(store)
    jm.create(
        handle=f"{state}-{error_code}",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    jm.finish(
        f"{state}-{error_code}",
        state=state,
        result='{"answer": 42}',
        error_code=error_code,
        raw_result='{"answer": 42}',
    )

    job = jm.get(f"{state}-{error_code}")
    assert job is not None
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_json_and_touch_rows_commit_together(store, tmp_path):
    from theater.daemon.jobs import JobManager, JobState
    from theater.daemon.schema import touch
    from theater.harness.base import EventPath

    touched = tmp_path / "touched.py"
    touched.write_text("before")
    jobs = JobManager(store)
    jobs.create(
        handle="json-touch",
        caller_id="cli",
        target_id="target",
        kind="send",
        cwd=str(tmp_path),
        response_format="json",
    )
    jobs.observe_paths("json-touch", (EventPath(path="touched.py", mode="write"),))
    touched.write_text("after")

    jobs.finish(
        "json-touch",
        state=JobState.DONE,
        result='{"ok": true}',
        raw_result='{"ok": true}',
    )

    job = jobs.get("json-touch")
    assert job is not None
    assert job.structured_result == '{"ok": true}'
    assert job.structured_status == "parsed"
    rows = list(store.conn.execute(touch.select().where(touch.c.job_handle == "json-touch")))
    assert len(rows) == 1
