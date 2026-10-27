"""Human-readable run summary and failure ledger for stdout."""

from __future__ import annotations

from .contract import Result
from .diagnostics import FAILURE_DETAIL_CHARS, clip_diagnostic
from .run import Run

_DETAIL_LIMIT = 160
#: Frame the ledger so a reader of a raw log can cut it out whole, wherever it sits.
LEDGER_BEGIN = "compat:failures:begin"
LEDGER_END = "compat:failures:end"


def failing_checks(result: Result, *, detail_limit: int = _DETAIL_LIMIT) -> list[str]:
    """One line per check that is not a pass or a declared skip, failures first."""
    bad = [check for check in result.checks if check.status in ("fail", "error")]
    bad.sort(key=lambda check: check.status != "fail")
    return [
        f"{check.name} ({check.status}): {clip_diagnostic(check.detail, detail_limit, tail=True)}"
        for check in bad
    ]


def render_run(run: Run) -> str:
    """Every release's result and admission at the start of this run."""
    lines = [
        f"### {run.harness}: {run.policy.range_text}",
        "",
        "| Version | Admitted | Verdict | First failing check |",
        "| --- | --- | --- | --- |",
    ]
    for result in run.results:
        first = (failing_checks(result) or ["—"])[0].replace("|", "\\|")
        admitted = "yes" if run.policy.admits(result.version) else "no"
        lines.append(f"| {result.version} | {admitted} | {result.verdict} | {first} |")
    return "\n".join(lines)


def render_failures(run: Run) -> str:
    """Every failure of this run in one block, or nothing when the run has none."""
    lines: list[str] = []
    for result in run.results:
        if result.verdict != "green":
            lines.append(f"{run.harness} {result.version} ended {result.verdict}:")
            lines += [
                f"  {check}" for check in failing_checks(result, detail_limit=FAILURE_DETAIL_CHARS)
            ]
    lines += [f"error: {error}" for error in run.errors]
    if unfinished := run.releases[len(run.results) :]:
        lines.append(f"unfinished: {', '.join(unfinished)}")
    return "\n".join([LEDGER_BEGIN, *lines, LEDGER_END]) if lines else ""


def error_annotation(title: str, message: str) -> str:
    """A GitHub Actions ``::error`` workflow command with its reserved characters escaped."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title={title}::{escaped}"
