"""GitHub-facing views of qualification summaries: job summary, overview, annotations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .diagnostics import clip_diagnostic

_MARKS = {"green": "✅", "red": "❌", "tooling": "🛠️", "inconclusive": "⚠️"}
_FAILING = ("red", "tooling", "inconclusive")


def _verdicts(summary: Mapping[str, Any]) -> set[str]:
    rows = [*summary["results"], *([summary["watch"]] if summary.get("watch") else [])]
    return {row["verdict"] for row in rows}


def overall(summary: Mapping[str, Any]) -> str:
    """The one verdict a harness run reduces to: its worst row, or ``no work``."""
    seen = _verdicts(summary)
    for verdict in _FAILING:
        if verdict in seen:
            return verdict
    return "green" if seen else "no work"


def _label(verdict: str) -> str:
    return f"{_MARKS.get(verdict, '')} {verdict}".strip()


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _action(summary: Mapping[str, Any]) -> str:
    publication = summary.get("publication")
    if publication is None:
        return "dry run"
    parts = []
    if publication["pr"]:
        parts.append("gate PR")
    if publication["issue"] != "none":
        parts.append(f"issue {publication['issue']}")
    return ", ".join(parts) or "none"


def _cause(row: Mapping[str, Any]) -> str:
    check = next((c for c in row.get("checks", []) if c["status"] in ("fail", "error")), None)
    return (
        clip_diagnostic(f"{check['name']}: {check.get('detail', '')}", 300, tail=True)
        if check
        else ""
    )


def render_harness(summary: Mapping[str, Any]) -> str:
    """One harness's job summary: gate, what was tried, every row with its attempts."""
    gate = summary.get("gate", {})
    lines = [
        f"### {summary['harness']} — {_label(overall(summary))}",
        "",
        f"- **Gate:** {summary['gate_kind']}, floor {gate.get('floor') or '—'}, "
        f"first unproved release {gate.get('ceiling') or '—'}",
        f"- **Candidates:** {', '.join(summary['candidates']) or 'none new'}"
        + (
            f" (deferred to the next run: {', '.join(summary['deferred'])})"
            if summary.get("deferred")
            else ""
        ),
        f"- **Promoted by this run:** {', '.join(summary['greens']) or 'none'}",
        f"- **Publication:** {_action(summary)}",
        *_composition(summary),
        "",
        "| Release | Role | Verdict | Attempts | Cause |",
        "| --- | --- | --- | --- | --- |",
    ]
    watch = summary.get("watch")
    for row in summary["results"]:
        role = (
            "candidate + watch"
            if watch and summary["watch_reused"] and watch["version"] == row["version"]
            else "candidate"
        )
        lines.append(_row(row, role))
    if watch and not summary["watch_reused"]:
        lines.append(_row(watch, "watch"))
    if not summary["results"] and not watch:
        lines.append("| — | — | no work | — | — |")
    lines.extend(_shadow(summary, watch))
    for warning in summary.get("stale_warnings", ()):
        lines.append(f"\nReview: {warning}")
    return "\n".join(lines)


def _shadow(summary: Mapping[str, Any], watch: Mapping[str, Any] | None) -> list[str]:
    """The live conformance suite's second opinion, listed beside the verdict and never in it."""
    rows = [
        (row["version"], row["shadow"])
        for row in [
            *summary["results"],
            *([watch] if watch and not summary["watch_reused"] else []),
        ]
        if row.get("shadow")
    ]
    if not rows:
        return []
    lines = [
        "",
        "**Shadow: live conformance suite (not part of the verdict)**",
        "",
        "| Release | Pass | Fail | Error | Skip | First problem |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for version, checks in rows:
        counts = {
            status: sum(1 for check in checks if check["status"] == status)
            for status in ("pass", "fail", "error", "skip")
        }
        problem = next((c for c in checks if c["status"] in ("fail", "error")), None)
        detail = (
            clip_diagnostic(f"{problem['name']}: {problem.get('detail', '')}", 200, tail=True)
            if problem
            else ""
        )
        lines.append(
            f"| {version} | {counts['pass']} | {counts['fail']} | {counts['error']} "
            f"| {counts['skip']} | {_cell(detail)} |"
        )
    return lines


def _composition(summary: Mapping[str, Any]) -> list[str]:
    log = summary.get("composition_attempts", [])
    if len(log) < 2:
        return []
    return [f"- **Composition:** {len(log)} attempts ({' → '.join(i['verdict'] for i in log)})"]


def _row(row: Mapping[str, Any], role: str) -> str:
    role = row.get("role", role)
    log = row.get("attempts", [])
    tried = sum(1 for item in log if item["verdict"] != "not run") or 1
    cause = _cause(row)
    if tried > 1:
        history = " → ".join(item["verdict"] for item in log)
        cause = f"{history}; {cause}" if cause else history
    return f"| {row['version']} | {role} | {_label(row['verdict'])} | {tried} | {_cell(cause)} |"


def render_overview(
    summaries: Sequence[Mapping[str, Any]], expected: Iterable[str] = ()
) -> tuple[str, list[str]]:
    """One table over every harness of a run; also the expected harnesses that left no result."""
    by_name = {summary["harness"]: summary for summary in summaries}
    names = sorted({*by_name, *expected})
    missing = [name for name in names if name not in by_name]
    lines = [
        "## Harness compatibility",
        "",
        "| Harness | Result | Candidates | Watch | Publication |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in names:
        summary = by_name.get(name)
        if summary is None:
            lines.append(f"| {name} | ❌ no result (the job did not finish) | — | — | — |")
            continue
        watch = summary.get("watch")
        watched = f"{watch['version']} {watch['verdict']}" if watch else "—"
        candidates = ", ".join(summary["candidates"]) or "—"
        lines.append(
            f"| {name} | {_label(overall(summary))} | {candidates} | {watched} "
            f"| {_action(summary)} |"
        )
    return "\n".join(lines), missing


def _escape(value: str, *, prop: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if prop else value


def error_annotation(title: str, message: str) -> str:
    return f"::error title={_escape(title, prop=True)}::{_escape(message)}"


def workflow_annotations(summary: Mapping[str, Any]) -> list[str]:
    """Workflow commands that surface each failing row on the run page."""
    title = _escape(f"Harness compatibility: {summary['harness']}", prop=True)
    commands = []
    for row in summary["issues"]:
        level = "warning" if row["verdict"] == "inconclusive" else "error"
        message = f"{row['version']} {row['verdict']}: {_cause(row) or 'see the job summary'}"
        commands.append(f"::{level} title={title}::{_escape(message)}")
    return commands
