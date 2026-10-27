"""The failures job lists every failure of a run from its jobs' logs and never fails the run."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.harness_compat.workflow_report import SUMMARY_BYTES, collect, run, summary


def _log(*lines: str) -> str:
    # A job log starts with a byte-order mark and stamps every line, colour codes included.
    stamped = [f"2026-10-27T19:15:{n:02d}.1234567Z {line}" for n, line in enumerate(lines)]
    return "﻿" + "\n".join(stamped)


def _job(job_id: int, name: str, status: str, conclusion: str | None, *steps: tuple[str, str]):
    steps_json = [{"name": n, "conclusion": c} for n, c in steps]
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "steps": steps_json,
    }


LEDGER = _log(
    "\x1b[36;1mrunning\x1b[0m",
    "compat:failures:begin",
    "vibe 3.0.2 ended red:",
    "  suite (fail): CAUSE ::error::injected",
    "compat:failures:end",
    "later output",
)
JOBS = [
    _job(1, "Verify vibe / 3.0.2", "completed", "success"),  # an excluded red leaves it green
    _job(2, "Verify pi / 1.4.0", "completed", "failure", ("uv sync", "failure")),
    _job(3, "Verify codex / Shards", "completed", "skipped"),
    _job(4, "Failures", "in_progress", None),  # this job itself
    _job(5, "Static checks", "completed", "success"),
    _job(6, "Verify claude / 2.1.0", "completed", "success"),
]
LOGS = {
    1: LEDGER,
    2: _log("starting", "E: Unable to locate package tmux"),
    5: _log("all fine"),
}


def _gh(*args: str) -> str:
    if "--paginate" in args:
        return json.dumps([{"jobs": JOBS[:3]}, {"jobs": JOBS[3:]}])
    job_id = int(args[1].split("/")[-2])
    if job_id not in LOGS:
        raise RuntimeError(f"gh api failed: no log for {job_id}")
    return LOGS[job_id]


def test_every_completed_job_with_a_failure_is_listed_and_an_unreadable_one_is_named() -> None:
    report = collect(_gh, "9", "1")

    assert report[:3] == [
        "Verify vibe / 3.0.2 (success):",
        "vibe 3.0.2 ended red:",
        "suite (fail): CAUSE ::error::injected",
    ]
    assert "Verify pi / 1.4.0 (failure):" in report
    assert "no failure ledger; failed steps: uv sync" in report
    assert "E: Unable to locate package tmux" in report
    assert "Verify claude / 2.1.0 (success):" in report  # its log could not be read
    assert any(line.startswith("log unavailable: ") for line in report)
    assert not any("Failures" in line or "Static checks" in line for line in report)


def test_the_report_is_replayed_inert_bounded_and_never_raises(tmp_path: Path, capsys) -> None:
    target = tmp_path / "summary.md"

    run(_gh, {"GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(target)})

    out = capsys.readouterr().out
    assert "  suite (fail): CAUSE ::error::injected" in out  # indented, so no workflow command
    assert not any(line.startswith("::") for line in out.splitlines())
    assert "CAUSE" in target.read_text()

    def broken(*args: str) -> str:
        raise RuntimeError("API unavailable")

    run(broken, {"GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(target)})
    assert "collector failed: RuntimeError: API unavailable" in capsys.readouterr().out
    huge = "x" * 1000 + "\n"
    assert len(summary("```\n" + huge * 2000).encode()) <= SUMMARY_BYTES + 200
    assert "more lines are in the job log" in summary("```\n" + huge * 2000)
