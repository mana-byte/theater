"""Awaited failures must explain whether retrying can duplicate work."""

import pytest

from tests.support.mcp_tools import resolved
from theater.daemon.rpc.jobs import _job_to_dict
from theater.mcp import tools
from theater.models import Job


@pytest.mark.parametrize(
    ("state", "code", "detail", "explanation", "keeps_result"),
    [
        (
            "crashed",
            "delivery_unknown",
            "WARNING: native work may still be running. Inspect the participant before re-sending.",
            "WARNING: native work may still be running. Inspect the participant before re-sending.",
            False,
        ),
        ("crashed", "daemon_restarted", "daemon restarted", "daemon restarted", False),
        ("killed", "unknown_code", "termination detail", "termination detail", False),
        ("crashed", "tmux_restarted", "old detail", "spawn_session(resume=<id>)", False),
        ("crashed", "prompt_never_seen", "old detail", "read_transcript before re-sending", False),
        ("crashed", "turn_failed", "backend failed", "see result for the detail", True),
        ("killed", "cancelled", "cancelled before its source ended", "nothing reached", True),
    ],
)
async def test_await_preserves_failure_guidance(state, code, detail, explanation, keeps_result):
    job = Job(
        handle="child#1",
        caller_id="p-me",
        target_id="child",
        kind="send",
        prompt="task",
        state=state,
        result=detail,
        error_code=code,
        created_at=1.0,
        finished_at=2.0,
    )
    session = resolved(**{"jobs.await": [_job_to_dict(job)]})

    [entry] = await tools.await_sessions(session, handles=[job.handle], max_wait=0)

    assert explanation in entry["error"]
    assert entry.get("result") == (detail if keeps_result else None)
