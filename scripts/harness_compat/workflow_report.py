"""One list of every failure of a workflow run: each completed job's ledger, or why it has none.

Presentation only: it reads with GET requests, decides no verdict and never fails the run it
reports on, so a failure it cannot read is named in the list rather than raised.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from .diagnostics import clean_diagnostic, exception_detail
from .publish import Gh
from .summary import LEDGER_BEGIN, LEDGER_END

#: A job log line starts with its timestamp, and the first one with a byte-order mark.
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
#: One step summary may not exceed 1 MiB.
SUMMARY_BYTES = 900 * 1024
_TAIL_LINES = 8


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
        try:
            # Job logs carry the runner's colour codes, which gh refuses to print otherwise.
            log = gh(
                "api",
                f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs",
                "--allow-escape-sequences",
            )
            # A green job can still hold failures: a verified release the policy excludes.
            found = _ledgers(log) or (_unframed(job, log) if job["conclusion"] != "success" else [])
        except Exception as exc:  # an unreadable log must not hide the others
            found = [f"log unavailable: {exception_detail(exc)}"]
        if found:
            report += [f"{job['name']} ({job['conclusion']}):", *found]
    return report


def render(report: list[str], *, collector_error: str | None = None) -> str:
    lines = [f"collector failed: {collector_error}"] if collector_error else []
    lines += report or ["no failure ledger in any completed job"]
    # Indented, so a replayed log line can never open with `::` and run as a workflow command.
    return "\n".join(f"  {line}" for line in lines)


def summary(text: str) -> str:
    """The text as a step summary: a code block, whole lines only, within the size limit."""
    kept: list[str] = []
    size = 0
    for line in text.replace("```", "'''").splitlines():
        size += len(line.encode()) + 1
        if size > SUMMARY_BYTES:
            kept.append(f"{len(text.splitlines()) - len(kept)} more lines are in the job log")
            break
        kept.append(line)
    return "## Failures of this run\n\n```text\n" + "\n".join(kept) + "\n```\n"


def run(gh: Gh, environ: Mapping[str, str]) -> None:
    error = None
    try:
        report = collect(gh, environ["GITHUB_RUN_ID"], environ.get("GITHUB_RUN_ATTEMPT", "1"))
    except Exception as exc:  # presentation only: say so, never fail the run
        report, error = [], exception_detail(exc)
    text = render(report, collector_error=error)
    print(text, flush=True)
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
    return found + ["(ledger incomplete: the job ended before it)"] * inside


def _unframed(job: Mapping, log: str) -> list[str]:
    """A failed job with no ledger died outside the harness code: its failed steps, last lines."""
    steps = [
        step["name"]
        for step in job.get("steps", [])
        if step["conclusion"] not in ("success", "skipped", None)
    ]
    tail = [line[:300] for line in map(_clean, log.splitlines()) if line][-_TAIL_LINES:]
    return [f"no failure ledger; failed steps: {'; '.join(steps) or job['conclusion']}", *tail]
