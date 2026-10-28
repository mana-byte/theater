"""One list of every failure of a workflow run: each finished job's ledger, failed steps and cause.

Presentation only: it reads with GET requests, decides no verdict and never fails the run it
reports on, so a failure it cannot read is named in the list rather than raised.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path

from .diagnostics import clean_diagnostic, exception_detail
from .publish import Gh
from .summary import LEDGER_BEGIN, LEDGER_END

#: A job log line starts with its timestamp, and the first one with a byte-order mark.
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
#: One step summary may not exceed 1 MiB.
SUMMARY_BYTES = 900 * 1024
#: The runner marks every failed step with a `##[error]` line; its cause is the output just before.
_ERROR_MARK = "##[error]"
_CONTEXT_LINES = 6


def collect(gh: Gh, run_id: str, attempt: str) -> list[str]:
    """The failures of every job of this run attempt that finished and was not skipped."""
    pages = json.loads(
        gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        )
    )
    report: list[str] = []
    for job in (job for page in pages for job in page["jobs"]):
        if job["status"] != "completed" or job["conclusion"] == "skipped":
            continue  # this job itself, or one that never ran
        green = job["conclusion"] == "success"
        found = [] if green else [_failed_steps(job)]
        try:
            # Job logs carry the runner's colour codes, which gh refuses to print otherwise.
            log = gh(
                "api",
                f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs",
                "--allow-escape-sequences",
            )
            # A green job can still hold failures: a verified release the policy excludes.
            # shortcut: a ledger hides a separate failed step's cause; its name stays in the list.
            found += _ledgers(log) or ([] if green else _around_errors(log))
        except Exception as exc:  # an unreadable log must not hide the others
            found.append(f"log unavailable: {exception_detail(exc)}")
        if found:
            report += [f"{job['name']} ({job['conclusion']}):", *found]
    return report


def render(report: list[str], *, collector_error: str | None = None) -> str:
    lines = [f"collector failed: {collector_error}"] if collector_error else []
    lines += report or ["no failure ledger in any completed job"]
    return "\n".join(lines)


def summary(text: str) -> str:
    """The text as a step summary: a code block, cut at the size limit."""
    body = text.replace("```", "'''").encode()
    if len(body) > SUMMARY_BYTES:
        body = body[:SUMMARY_BYTES] + b"\n... cut here; the rest is in the job log"
    return "## Failures of this run\n\n```text\n" + body.decode(errors="ignore") + "\n```\n"


def run(gh: Gh, environ: Mapping[str, str]) -> None:
    error = None
    try:
        report = collect(gh, environ["GITHUB_RUN_ID"], environ.get("GITHUB_RUN_ATTEMPT", "1"))
    except Exception as exc:  # presentation only: say so, never fail the run
        report, error = [], exception_detail(exc)
    text = render(report, collector_error=error)
    # Replayed log text may hold workflow commands, in either syntax, at any indentation.
    token = uuid.uuid4().hex
    print(f"::stop-commands::{token}", flush=True)
    try:
        print(text, flush=True)
    finally:
        print(f"::{token}::", flush=True)
    if path := environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(summary(text))


def _clean(line: str) -> str:
    return clean_diagnostic(_STAMP.sub("", line))


def _ledgers(log: str) -> list[str]:
    found: list[str] = []
    inside = False
    for raw in log.splitlines():
        line = _clean(raw)
        if line == LEDGER_BEGIN:
            inside = True
        elif line == LEDGER_END:
            inside = False
        elif inside:
            found.append(line)
    if inside:
        found.append("(ledger incomplete: the job ended before it)")
    return found


def _failed_steps(job: Mapping) -> str:
    steps = [
        f"{step['name']} ({step['conclusion']})"
        for step in job.get("steps", [])
        if step["conclusion"] not in ("success", "skipped", None)
    ]
    return f"failed steps: {'; '.join(steps) or job['conclusion']}"


def _around_errors(log: str) -> list[str]:
    """Each failed step's last output: the lines before the runner's error marker, and it."""
    lines = [line for line in map(_clean, log.splitlines()) if line]
    found: list[str] = []
    taken = 0
    for index, line in enumerate(lines):
        if line.startswith(_ERROR_MARK):
            found += lines[max(index - _CONTEXT_LINES, taken) : index + 1]
            taken = index + 1
    return found
