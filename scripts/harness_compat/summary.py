"""Human-readable run summary for stdout: the log is the only place results are shown."""

from __future__ import annotations

from .contract import Result
from .diagnostics import clip_diagnostic
from .run import Run

_DETAIL_LIMIT = 160


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


def error_annotation(title: str, message: str) -> str:
    """A GitHub Actions ``::error`` workflow command with its reserved characters escaped."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title={title}::{escaped}"
