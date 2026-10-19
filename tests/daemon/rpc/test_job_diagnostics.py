"""Job RPCs expose only bounded strict-failure diagnostics and stable recovery ids."""

from __future__ import annotations

import json

import pytest

from theater.constants.daemon import TMUX_RESTART_JOB_ERROR_CODE
from theater.daemon.response_contracts.contract import StrictContract
from theater.models import JobState


async def test_strict_failure_projects_validation_issues_only(client, daemon, monkeypatch):
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string", "maxLength": 1500}},
    }
    fmt = StrictContract("custom", json.dumps(schema)).envelope()
    long_fmt = StrictContract(
        "custom",
        json.dumps(
            {"type": "object", "properties": {"summary": {"type": "string", "maxLength": 5000}}}
        ),
    ).envelope()
    long_answer = json.dumps({"summary": "x" * 2500})
    good = daemon.jobs.create(
        handle="target#good",
        caller_id="caller",
        target_id="target",
        kind="send",
        response_format=long_fmt,
    )
    daemon.jobs.finish(
        good.handle,
        state=JobState.DONE,
        result=long_answer[:2000],
        raw_result=long_answer,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            daemon.store,
            "contract_audit_for_job",
            lambda handle: (_ for _ in ()).throw(AssertionError(f"audit queried for {handle}")),
        )
        valid = await client.call("jobs.status", handle=good.handle)
    assert valid["structured_status"] == "parsed"
    assert valid["structured_result"] == long_answer
    assert "structured_error" not in valid

    bad = daemon.jobs.create(
        handle="target#bad",
        caller_id="caller",
        target_id="target",
        kind="send",
        response_format=fmt,
    )
    answer = json.dumps({"summary": "SECRET" + "x" * 1516})
    daemon.jobs.finish(bad.handle, state=JobState.DONE, result=answer[:2000], raw_result=answer)
    row = await client.call("jobs.status", handle=bad.handle)
    assert row["structured_status"] == "unable_to_parse"
    assert row["structured_error"] == {
        "event": "unsupported_route",
        "reason": "schema",
        "issues": [{"path": "/summary", "keyword": "maxLength", "limit": 1500, "actual": 1522}],
    }
    assert "SECRET" not in json.dumps(row["structured_error"])
    awaited = await client.call("jobs.await", handles=[bad.handle], max_wait=0)
    assert awaited[0]["structured_error"] == row["structured_error"]


async def test_tmux_recovery_hint_uses_the_job_target_id(client, daemon):
    job = daemon.jobs.create(
        handle="target#17", caller_id="caller", target_id="target", kind="spawn"
    )
    daemon.jobs.finish(job.handle, state=JobState.CRASHED, error_code=TMUX_RESTART_JOB_ERROR_CODE)
    row = await client.call("jobs.status", handle=job.handle)
    assert row["target_id"] == "target"
    assert "resume=<target_id>" in row["error"]
    assert "split" not in row["error"] and "up to" not in row["error"]


@pytest.mark.parametrize(
    ("answer", "reason", "keyword"),
    [
        ("SECRET malformed", "not_json", "json"),
        ('"' + "x" * 65_536 + '"', "too_large", "maxLength"),
    ],
)
async def test_rpc_failure_uses_the_raw_answer_for_safe_diagnostics(
    client, daemon, answer, reason, keyword
):
    fmt = StrictContract("custom", '{"type":"string"}').envelope()
    job = daemon.jobs.create(
        handle=f"target#{reason}",
        caller_id="caller",
        target_id="target",
        kind="send",
        response_format=fmt,
    )
    daemon.jobs.finish(job.handle, state=JobState.DONE, result=answer[:2000], raw_result=answer)
    row = await client.call("jobs.status", handle=job.handle)
    issue = row["structured_error"]["issues"][0]
    assert row["structured_error"]["reason"] == reason
    assert issue["path"] == "/" and issue["keyword"] == keyword
    if reason == "too_large":
        assert (issue["limit"], issue["actual"]) == (65_536, len(answer))
    else:
        assert issue["position"] == 0
    assert "SECRET" not in json.dumps(row["structured_error"])
    detail = daemon.store.contract_audit_for_job(job.handle)[-1][1]
    assert detail is not None and len(detail) <= 500
    assert json.loads(detail)["reason"] == reason
