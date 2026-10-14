"""Deterministic publication copy for harness compatibility runs."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .contract import Result, Route, version_key
from .diagnostics import clip_diagnostic

_CHECK_LIMIT = 750
_ROW_LIMIT = 1600


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
    evidence_only: bool = False,
    stale_warnings: Iterable[str] = (),
) -> PublishReport:
    """Build one rolling PR/issue report; inconclusive results cause neither."""
    if any(result.harness != harness for result in results):
        raise ValueError("every result must match the report harness")

    ordered = tuple(sorted(results, key=lambda result: version_key(result.version)))
    ordered_routes = tuple(sorted((method.upper(), path) for method, path in routes))
    green = tuple(result for result in ordered if result.verdict == "green")
    failing = tuple(result for result in ordered if result.verdict in {"red", "tooling"})
    admitted = frozenset(admitted_versions)
    inconclusive = any(result.verdict == "inconclusive" for result in ordered)
    labels = [f"compat:{harness}"]
    if any(result.verdict == "tooling" for result in failing):
        labels.append("tooling")
    if any(result.verdict == "red" and result.version in admitted for result in failing):
        labels.append("admitted-regression")

    pr_body = _body(f"Qualified {harness} releases", green, run_url=run_url, routes=ordered_routes)
    warnings = sorted(set(stale_warnings))
    if warnings:
        lines = [f"- {_markdown_text(item)}" for item in warnings[:20]]
        if len(warnings) > 20:
            lines.append(f"- … and {len(warnings) - 20} more")
        pr_body += "\n\nHistorical references to the previous limit remain (review):\n" + "\n".join(
            lines
        )
    if evidence_only:
        pr_body += (
            "\n\nCompatibility ceiling was NOT raised. This PR records evidence only; "
            "a human decides whether to raise the ceiling."
        )
    return PublishReport(
        harness=harness,
        pr_needed=bool(green) and not inconclusive,
        pr_title=f"chore(compat): update {harness} compatibility evidence",
        pr_body=pr_body,
        issue_action=(
            "none" if inconclusive else "upsert" if failing else "close" if green else "none"
        ),
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
    run = f"[workflow run]({_markdown_url(run_url)})" if run_url else "not available"
    rows = ["| Version | Verdict | Checks | Run |", "| --- | --- | --- | --- |"]
    rows.extend(
        f"| {_cell(result.version)} | {result.verdict} | {_checks(result)} | {run} |"
        for result in results
    )
    if not results:
        rows.append(f"| — | — | — | {run} |")

    counts = Counter(routes)
    route_lines = [
        f"- `{method.upper()} {_markdown_text(path)}` ×{count}"
        for (method, path), count in sorted(counts.items())
    ]
    if not route_lines:
        route_lines.append("- None recorded")
    return "\n".join(
        [
            f"## {heading}",
            "",
            *rows,
            "",
            "Routes:",
            *route_lines,
        ]
    )


def _checks(result: Result) -> str:
    if not result.checks:
        return "—"
    ordered = sorted(result.checks, key=lambda check: check.status not in ("fail", "error"))
    rendered = []
    for check in ordered:
        prefix = clip_diagnostic(f"{check.name}: {check.status}", 200)
        detail = (
            " — "
            + clip_diagnostic(
                check.detail,
                _CHECK_LIMIT - len(prefix) - 3,
                tail=check.status in ("fail", "error"),
            )
            if check.detail
            else ""
        )
        rendered.append(_cell(prefix + detail))
    return clip_diagnostic("<br>".join(rendered), _ROW_LIMIT)


def _cell(value: str) -> str:
    return _markdown_text(value).replace("|", "\\|")


def _markdown_text(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("`", "\\`")


def _markdown_url(value: str) -> str:
    return value.replace(" ", "%20").replace(")", "%29").replace("(", "%28")
