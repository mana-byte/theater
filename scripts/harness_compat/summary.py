"""Human-readable run summary for stdout: the log is the only place results are shown."""

from __future__ import annotations

from theater.harness.contracts.version_policy import VersionPolicy

from .contract import Result
from .diagnostics import clip_diagnostic
from .policy import Mismatch
from .run import Run

_DETAIL_LIMIT = 160


def failing_checks(result: Result) -> list[str]:
    """One line per check that is not a pass or a declared skip, failures first."""
    bad = [check for check in result.checks if check.status in ("fail", "error")]
    bad.sort(key=lambda check: check.status != "fail")
    return [
        f"{check.name} ({check.status}): {clip_diagnostic(check.detail, _DETAIL_LIMIT, tail=True)}"
        for check in bad
    ]


def render_run(run: Run, mismatches: list[Mismatch]) -> str:
    """A table of every release with its admission, then what disagrees with the policy."""
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
    if mismatches:
        lines.extend(["", "Disagreements with the policy:"])
        lines.extend(
            f"- `{item.version}`: {item.kind.replace('-', ' ')} ({item.verdict})"
            for item in mismatches
        )
    return "\n".join(lines)


def change_text(old: VersionPolicy, new: VersionPolicy) -> str:
    return f"`{old.range_text}` to `{new.range_text}`"


def error_annotation(title: str, message: str) -> str:
    """A GitHub Actions ``::error`` workflow command with its reserved characters escaped."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error title={title}::{escaped}"
