"""The failures job lists every failure of a run from its jobs' logs and never fails the run."""

from __future__ import annotations

import json
from pathlib import Path

from harness_compat.workflow_report import SUMMARY_BYTES, collect, run, summary


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
    # A red ledger and, apart from it, a failed step.
    _job(7, "Verify codex / 0.164.0", "completed", "failure", ("Post checkout", "failure")),
]
LOGS = {
    1: LEDGER,
    # The runner's marker comes after the step's output and before its own cleanup.
    2: _log(
        "starting",
        "E: Unable to locate package tmux",
        "##[error]Process completed with exit code 100.",
        *(f"cleanup {number}" for number in range(12)),
    ),
    5: _log("all fine"),
    7: LEDGER,
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
    # No ledger: the failed step and what it printed last, not the cleanup that follows.
    pi = report.index("Verify pi / 1.4.0 (failure):")
    assert report[pi + 1 : pi + 5] == [
        "failed steps: uv sync (failure)",
        "starting",
        "E: Unable to locate package tmux",
        "##[error]Process completed with exit code 100.",
    ]
    assert "cleanup 11" not in report
    # A ledger does not hide the failed step beside it.
    codex = report.index("Verify codex / 0.164.0 (failure):")
    assert report[codex + 1 : codex + 3] == [
        "failed steps: Post checkout (failure)",
        "vibe 3.0.2 ended red:",
    ]
    assert "Verify claude / 2.1.0 (success):" in report  # its log could not be read
    assert any(line.startswith("log unavailable: ") for line in report)
    assert not any("Failures" in line or "Static checks" in line for line in report)


def test_the_report_is_replayed_inert_bounded_and_never_raises(tmp_path: Path, capsys) -> None:
    target = tmp_path / "summary.md"

    run(_gh, {"GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(target)})

    # The runner trims leading spaces and matches `##[error]` anywhere, so only stopping
    # command processing makes replayed text inert.
    lines = capsys.readouterr().out.splitlines()
    token = lines[0].removeprefix("::stop-commands::")
    assert lines[0] != token and lines[-1] == f"::{token}::"
    assert "suite (fail): CAUSE ::error::injected" in lines[1:-1]
    assert "##[error]Process completed with exit code 100." in lines[1:-1]
    assert "CAUSE" in target.read_text()

    def broken(*args: str) -> str:
        raise RuntimeError("API unavailable")

    run(broken, {"GITHUB_RUN_ID": "9", "GITHUB_STEP_SUMMARY": str(target)})
    assert "collector failed: RuntimeError: API unavailable" in capsys.readouterr().out
    cut = summary("```\n" + "x" * (2 * SUMMARY_BYTES))
    assert len(cut.encode()) <= SUMMARY_BYTES + 200 and "the rest is in the job log" in cut
