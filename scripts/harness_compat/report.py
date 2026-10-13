"""Deterministic publication copy for harness compatibility runs."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .contract import Result, Route, version_key


@dataclass(frozen=True, slots=True)
class PublishReport:
    """Everything the CLI must expose to the publishing workflow."""

    harness: str
    pr_needed: bool
    pr_title: str
    pr_body: str
    issue_action: str
    issue_title: str
    issue_body: str
    issue_labels: tuple[str, ...]

    def github_outputs(self) -> dict[str, str]:
        """Values consumed by ``harness-compat.yml``."""
        return {
            "pr_needed": str(self.pr_needed).lower(),
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "issue_action": self.issue_action,
            "issue_title": self.issue_title,
            "issue_body": self.issue_body,
            "issue_labels": ",".join(self.issue_labels),
        }


def build_report(
    harness: str,
    results: Sequence[Result],
    *,
    run_url: str,
    routes: Iterable[Route] = (),
    admitted_versions: Iterable[str] = (),
) -> PublishReport:
    """Build one rolling PR/issue report; inconclusive results cause neither."""
    if any(result.harness != harness for result in results):
        raise ValueError("every result must match the report harness")

    ordered = tuple(sorted(results, key=lambda result: version_key(result.version)))
    ordered_routes = tuple(sorted({(method.upper(), path) for method, path in routes}))
    green = tuple(result for result in ordered if result.verdict == "green")
    failing = tuple(result for result in ordered if result.verdict in {"red", "tooling"})
    admitted = frozenset(admitted_versions)
    labels = [f"compat:{harness}"]
    if any(result.verdict == "tooling" for result in failing):
        labels.append("tooling")
    if any(result.verdict == "red" and result.version in admitted for result in failing):
        labels.append("admitted-regression")

    return PublishReport(
        harness=harness,
        pr_needed=bool(green),
        pr_title=f"chore(compat): update {harness} compatibility evidence",
        pr_body=_body(
            f"Qualified {harness} releases", green, run_url=run_url, routes=ordered_routes
        ),
        issue_action="upsert" if failing else "close" if green else "none",
        issue_title=f"Harness compatibility failures: {harness}",
        issue_body=_body(
            f"Failing {harness} releases", failing, run_url=run_url, routes=ordered_routes
        ),
        issue_labels=tuple(labels),
    )


def write_github_outputs(report: PublishReport, path: Path) -> None:
    """Append report fields using GitHub's multiline output-file syntax."""
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in report.github_outputs().items():
            marker = f"HARNESS_COMPAT_{key.upper()}"
            if marker in value:
                raise ValueError(f"output {key!r} contains reserved marker {marker!r}")
            stream.write(f"{key}<<{marker}\n{value}\n{marker}\n")


def _body(
    heading: str,
    results: Sequence[Result],
    *,
    run_url: str,
    routes: Iterable[Route],
) -> str:
    rows = ["| Version | Verdict | Checks |", "| --- | --- | --- |"]
    rows.extend(
        f"| {_cell(result.version)} | {result.verdict} | {_checks(result)} |" for result in results
    )
    if not results:
        rows.append("| — | — | — |")

    route_lines = [
        f"- `{method.upper()} {_markdown_text(path)}`"
        for method, path in sorted(set(routes), key=lambda route: (route[0].upper(), route[1]))
    ]
    if not route_lines:
        route_lines.append("- None recorded")
    run = f"[workflow run]({_markdown_url(run_url)})" if run_url else "not available"
    return "\n".join(
        [
            f"## {heading}",
            "",
            *rows,
            "",
            f"Run: {run}",
            "",
            "Routes:",
            *route_lines,
        ]
    )


def _checks(result: Result) -> str:
    if not result.checks:
        return "—"
    return "<br>".join(
        _cell(f"{check.name}: {check.status}" + (f" — {check.detail}" if check.detail else ""))
        for check in result.checks
    )


def _cell(value: str) -> str:
    return _markdown_text(value).replace("|", "\\|")


def _markdown_text(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("`", "\\`")


def _markdown_url(value: str) -> str:
    return value.replace(" ", "%20").replace(")", "%29").replace("(", "%28")
